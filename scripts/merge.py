#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
merge.py —— 全部 part 为 DONE 后，严格按 manifest 顺序合并生成 <原名>.zh.txt

输出名以源文档名为准（如源 断锁.txt -> 断锁.zh.txt），由 _paths.py 推导。

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
    python scripts/merge.py --strict        # 存在 needs_human_review 的 part 也拒绝合并
    python scripts/merge.py --check         # 只校验现有最终译文，不重写
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

# ROOT 由【源文档位置】推导，而非脚本位置（详见 _paths.py）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import (  # noqa: E402
    ROOT,
    SOURCE,
    MANIFEST_PATH,
    STATE_PATH,
    PARTS_OUT_DIR,
    OUT_TXT_PATH,
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


def main() -> int:
    setup_io()
    ap = argparse.ArgumentParser(description="合并译文")
    ap.add_argument("--source", metavar="PATH", help="源文档路径（默认按 _paths.py 优先级定位）")
    ap.add_argument("--check", action="store_true", help="只校验现有译文，不重写")
    ap.add_argument("--strict", action="store_true", help="存在待复核 part 时拒绝合并")
    args = ap.parse_args()

    if not MANIFEST_PATH.exists() or not STATE_PATH.exists():
        sys.exit("FATAL: 缺少 manifest.json 或 state.json")
    manifest = load_json(MANIFEST_PATH)
    state = load_json(STATE_PATH)

    if OUT_TXT_PATH.exists() and args.check:
        text, stats = build_translated(manifest)
        cur = OUT_TXT_PATH.read_bytes()
        exp = text.encode("utf-8")
        print("=" * 68)
        print(f"check result: {'IDENTICAL' if cur == exp else 'DIFFERENT'}")
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

    text, stats = build_translated(manifest)
    data = text.encode("utf-8")
    write_bytes_atomic(OUT_TXT_PATH, data)

    print("=" * 68)
    print(f"merged -> {OUT_TXT_PATH}  ({len(data)} bytes)")
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
