#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
merge.py —— 全部 part 为 DONE 后，严格按 manifest 顺序合并生成最终译文

输出命名（路径由 _paths.py 的 out_txt_path() 解析）：
    源文件名译一遍 -> 译名 != 原名  => `<目标语言书名>.txt`
                      译名 == 原名  => `<原名>_translated.txt`（回退）

目标语言书名 = 源文件名（原标题）的目标语言翻译。优先 --title；没传就用 state.json 里已存的；
都没有且配了 NOVEL_LLM_* 时，脚本自己调 LLM 译一遍源文件名（目标语言取 NOVEL_TARGET_LANG）。
书名清洗后写入 state.json 的 output.title_cn，后续重跑 merge / --check 复用同一文件名。

铁律
----
1. 只有 state 中所有 part 均为 DONE、且 current_part_id == null 才允许合并。
2. 顺序严格取自 manifest.parts，**不按文件系统排序、不按修改时间排序**。
3. 段落之间有多少个换行，完全由 manifest 记录的 sep_newlines 决定；
   版式由程序确定性重建，不依赖 LLM 是否保留了空行。
4. 不自动删除、不自动修正、不自动补充任何正文内容；缺失即报错停止。

用法
----
    python scripts/merge.py
    python scripts/merge.py --title "目标语言书名"   # 指定目标语言书名（源文件名原标题的译文）
    python scripts/merge.py --strict        # 存在 needs_human_review 的 part 也拒绝合并
    python scripts/merge.py --check         # 只校验现有最终译文，不重写
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

# ROOT 由【源文档位置】推导，而非脚本位置（详见 _paths.py）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import (  # noqa: E402
    ROOT,
    SOURCE,
    MANIFEST_PATH,
    STATE_PATH,
    PARTS_OUT_DIR,
    fallback_name,
    out_txt_path,
    sanitize_title,
)


def setup_io() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_text(path: Path) -> str:
    data = path.read_bytes()
    text = data.decode("utf-8-sig")
    return text[1:] if text and text[0] == "\ufeff" else text


def precheck(manifest: dict, state: dict, strict: bool) -> list[str]:
    errs = []
    if state.get("source_sha256") != manifest["source"]["sha256"]:
        errs.append("state 与 manifest 的原文 SHA256 不一致")
    probe = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if state.get("manifest_sha256") != sha256_bytes(canonical_json_bytes(probe)):
        errs.append("state 与 manifest 的 manifest_sha256 不一致")
    if state.get("part_count") != manifest.get("part_count"):
        errs.append("part_count 不一致")

    not_done = [p["part_id"] for p in manifest["parts"]
                if state["parts"].get(p["part_id"], {}).get("status") != "DONE"]
    if not_done:
        errs.append(f"以下 part 尚未 DONE：{', '.join(not_done[:10])}"
                    f"{' …' if len(not_done) > 10 else ''}")
    if state.get("current_part_id") is not None:
        errs.append(f"current_part_id = {state['current_part_id']}，未完成")

    for p in manifest["parts"]:
        pid = p["part_id"]
        op = PARTS_OUT_DIR / f"{pid}.txt"
        if not op.exists():
            errs.append(f"缺少译文 {op.name}")
            continue
        disk = sha256_bytes(op.read_bytes())
        rec = state["parts"].get(pid, {}).get("output_sha256")
        if rec and disk != rec:
            errs.append(f"{pid} 译文内容与 state 记录的 output_sha256 不一致")
        if strict and state["parts"].get(pid, {}).get("needs_human_review"):
            errs.append(f"{pid} 处于待人工复核状态（--strict 拒绝合并）")
    return errs


def build_translated(manifest: dict) -> tuple[str, dict]:
    """按 manifest 顺序 + sep_newlines 重建全文版式。"""
    buf: list[str] = []
    total_units = 0
    total_src = 0
    total_out = 0
    per_part = []
    for p in manifest["parts"]:
        pid = p["part_id"]
        op = PARTS_OUT_DIR / f"{pid}.txt"
        lines = [ln.strip() for ln in read_text(op).split("\n") if ln.strip()]
        paras = p.get("paragraphs", [])
        if len(lines) != len(paras):
            raise SystemExit(
                f"FATAL: {pid} 译文行数 {len(lines)} != 原文段落数 {len(paras)}；"
                f"禁止合并，请先修正该 part"
            )
        for ln, para in zip(lines, paras):
            buf.append("\n" * int(para.get("sep_newlines", 0)) + ln)
            total_src += para.get("char_count", 0)
            total_out += len(ln)
        total_units += len(lines)
        per_part.append((pid, len(lines), total_out))
    tail = int(manifest["parts"][-1].get("tail_newlines", 0))
    text = "".join(buf) + "\n" * tail
    stats = {
        "units": total_units,
        "source_chars": total_src,
        "translated_chars": total_out,
        "ratio": round(total_out / total_src, 4) if total_src else 0.0,
        "tail_newlines": tail,
        "per_part": per_part,
    }
    return text, stats


def save_state(state: dict) -> None:
    """写回 state.json（与 resume.py 一致的格式：原子写 + updated_at）。"""
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["updated_by"] = "merge.py"
    write_bytes_atomic(
        STATE_PATH, (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def title_system_prompt() -> str:
    """根据 NOVEL_TARGET_LANG 生成书名翻译的系统提示词（通用、语言无关）。"""
    tgt = os.environ.get("NOVEL_TARGET_LANG") or "目标语言"
    return (
        f"你是书名译者。把给定的小说文件名译成{tgt}书名。\n"
        f"规则：输出{tgt}书名；保留卷次与作者，只清掉下载站后缀之类的脏数据；"
        "不要解释、不要引号、不要 .txt 扩展名；无需翻译时原样输出。"
    )


def apply_title(state: dict, raw_title: str) -> bool:
    """
    把目标语言书名（--title 传入，或 LLM 译出）清洗后写入 state.json 的 output 块。

    返回 True 表示采用该书名；下列情况返回 False，由调用方走回退命名：
      - 清洗后为空
      - 译名与原名相同（翻译后跟原名一样 —— 按规则此时用 <原名>_translated.txt）
    """
    title = sanitize_title(raw_title)
    if title is None or title == SOURCE.stem:
        return False
    state["output"] = {"title_cn": title, "file": f"{title}.txt"}
    save_state(state)
    return True


def auto_translate_title(state: dict) -> bool:
    """
    没传 --title 且 state 里也没有书名时，配了 NOVEL_LLM_* 就让 LLM 把源文件名译一遍。

    失败（未配置 / 调用异常）一律返回 False，静默走回退命名——合并本身不能被书名拖住。
    """
    if not (os.environ.get("NOVEL_LLM_BASE_URL") and os.environ.get("NOVEL_LLM_API_KEY")
            and os.environ.get("NOVEL_LLM_MODEL")):
        return False
    try:
        from run_agent import call_llm
        raw, _meta = call_llm(title_system_prompt(), SOURCE.stem, 200)
    except Exception as e:
        print(f"  书名自动翻译失败（{e}），改用回退命名", file=sys.stderr)
        return False
    return apply_title(state, raw)


def resolve_check_path() -> tuple[Path, str]:
    """
    --check 时定位「现有最终译文」：优先目标语言书名路径，
    不存在而回退名存在（旧产物）时改用回退路径。
    """
    path, origin = out_txt_path()
    if origin == "title" and not path.exists():
        fb = ROOT / fallback_name(SOURCE)
        if fb.exists():
            return fb, "fallback"
    return path, origin


def main() -> int:
    setup_io()
    ap = argparse.ArgumentParser(description="合并译文")
    ap.add_argument("--source", metavar="PATH", help="源文档路径（默认按 _paths.py 优先级定位）")
    ap.add_argument("--title", metavar="目标语言书名",
                    help="目标语言书名（源文件名原标题的译文）；译名与原名相同时改用回退名。"
                         "省略则沿用 state.json 中已保存的书名，都没有时脚本自己译一遍源文件名")
    ap.add_argument("--check", action="store_true", help="只校验现有译文，不重写")
    ap.add_argument("--strict", action="store_true", help="存在待复核 part 时拒绝合并")
    args = ap.parse_args()

    if not MANIFEST_PATH.exists() or not STATE_PATH.exists():
        sys.exit("FATAL: 缺少 manifest.json 或 state.json")
    manifest = load_json(MANIFEST_PATH)
    state = load_json(STATE_PATH)

    if args.title and not apply_title(state, args.title):
        # 显式传入的译名不可用（空 / 与原名相同）时以它为准：清掉旧书名，走回退命名
        if "output" in state:
            state.pop("output")
            save_state(state)
        print("  提示：译名与原名相同或不可用，改用回退命名")

    if args.check:
        check_path, origin = resolve_check_path()
        if not check_path.exists():
            sys.exit(f"FATAL: 找不到最终译文 {check_path}，请先执行合并")
        text, stats = build_translated(manifest)
        cur = check_path.read_bytes()
        exp = text.encode("utf-8")
        print("=" * 68)
        print(f"check result: {'IDENTICAL' if cur == exp else 'DIFFERENT'}")
        print(f"  最终译文        : {check_path.name}")
        print(f"  命名来源        : {origin}")
        print(f"  最终译文 sha256 : {sha256_bytes(cur)}")
        print(f"  expected      sha256 : {sha256_bytes(exp)}")
        print(f"  units={stats['units']} ratio={stats['ratio']} (仅供参考，不代表完整)")
        print("=" * 68)
        return 0 if cur == exp else 2

    errs = precheck(manifest, state, args.strict)
    if errs:
        print("FATAL: 前置条件不满足，拒绝合并：", file=sys.stderr)
        for e in errs:
            print("  " + e, file=sys.stderr)
        print("\n提示：完成条件只有一种 —— 所有 part 均为 DONE 且 current_part_id == null。",
              file=sys.stderr)
        return 2

    # 书名还没定时，配了 LLM 就让脚本自己把源文件名译一遍
    out_path, origin = out_txt_path()
    if origin == "fallback" and not args.title and auto_translate_title(state):
        out_path, origin = out_txt_path()

    text, stats = build_translated(manifest)
    data = text.encode("utf-8")
    write_bytes_atomic(out_path, data)

    print("=" * 68)
    print(f"merged -> {out_path}  ({len(data)} bytes)")
    print(f"  命名来源  : {origin}"
          + ("（目标语言书名，来自 --title / state.json）" if origin == "title"
          else "（回退名：译名与原名相同或未提供，可用 --title 指定）"))
    print(f"  units={stats['units']}  source_chars={stats['source_chars']}  "
          f"translated_chars={stats['translated_chars']}")
    print(f"  ratio={stats['ratio']}（仅参考，字符比例不能证明翻译完整）")
    print(f"  sha256={sha256_bytes(data)}")
    print("=" * 68)
    for pid, n, _tot in stats["per_part"]:
        print(f"  {pid}: {n} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
