#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fix_quotes.py —— 标点规范化入口（按语言对选择策略）

本脚本是**通用入口**，不内置任何"放之四海皆准"的引号规则：

    fix_quotes.py
        ↓
    读取 NOVEL_SOURCE_LANG / NOVEL_TARGET_LANG（经 _lang.lang_pair 归一）
        ↓
    在 HANDLERS 中查找 (源语言, 目标语言)
        ├── 命中     -> 执行该语言对的标点规范化
        └── 未命中   -> 安全跳过（NO-OP），不猜测、不回退、不报错

当前内置的语言对：
    ja → zh-hans（简体中文；含 zh / zh-CN / zh-Hans 写法）

新增语言对只需写一个 handler 并在 HANDLERS 注册，不必改动主流程。

共同机制（与语言无关）
----------------------
- 依据 manifest.json 的「原文段落 <-> 译文行」1:1 对齐逐段处理，不靠正则猜
- dry-run 默认；`--apply` 才写回 .translate/parts_out/ 并刷新 state.json

用法
----
    python scripts/fix_quotes.py             # dry-run，只报告
    python scripts/fix_quotes.py --apply     # 写回 .translate/parts_out/ 并刷新 state.json
                                             # （随后需重跑 merge.py）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# ROOT 由【源文档位置】推导，而非脚本位置（详见 _paths.py）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import (  # noqa: E402
    ROOT,
    SOURCE,
    ORIGINAL_PATH,
    MANIFEST_PATH,
    STATE_PATH,
    PARTS_OUT_DIR,
)
from _lang import lang_pair  # noqa: E402  语言对归一（不重新实现语言解析）

L_NIJU, R_NIJU = "\u300e", "\u300f"   # 『 』
L_KAK, R_KAK = "\u300c", "\u300d"     # 「 」
L_SIN, R_SIN = "\u2018", "\u2019"     # ‘ ’
L_CUR, R_CUR = "\u201c", "\u201d"     # “ ”
L_BOOK = "\u300a"                     # 《


def setup_io() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def read_text(p: Path) -> str:
    t = p.read_bytes().decode("utf-8-sig")
    return t[1:] if t and t[0] == "\ufeff" else t


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------
# 语言对 handler：ja → zh-hans（简体中文）
# --------------------------------------------------------------------------
def fix_ja_zh_hans(src: str, cur: str) -> tuple[str, list[str]]:
    """
    日文原文 → 简体中文译文的引号规范化（方案 B：中文规范优先）。

    逐段依据「原文该处符号的角色」决定译文符号：
    A1 『』嵌套在「」之内（引用中的引用） -> 中文规范：内层单引号 ‘...’
    A2 『』为书名 / 作品名                -> 书名号 《...》（已正确的不动）
    A3 『』独立成段（广播 / 公告 / 独立引用，全段无「」）
                                         -> 保留 『...』，以区别于人物对话
       仅当「整段就是一个 『...』」且译文只有一对 “...” 时才整段转 『』；
       长叙述段中嵌入的『』保留译文现有处理，避免误改同段并存的对话引号。
    B  日文直角引号 「」 -> 中文弯引号 “”
       简体中文不使用「」，一律转 “”；A 处理后嵌套情形自然成为 “…‘…’…”。

    返回 (处理后的译文行, 标签列表)，标签供主流程统计与取样：
        has-niju           原文段落含『』（只用于统计，不进样本）
        nested->''         A1 命中
        standalone->niju   A3 命中
        kakko->curly       B 命中
    """
    tags: list[str] = []

    # ---------- A. 『』 规则 ----------
    if L_NIJU in src:
        tags.append("has-niju")
        if L_KAK in src:
            # 1) 嵌套引用 -> 中文内层单引号
            if L_NIJU in cur:
                cur = cur.replace(L_NIJU, L_SIN).replace(R_NIJU, R_SIN)
                tags.append("nested->''")
        else:
            # 2)(3) 独立引用
            if not (L_NIJU in cur or L_BOOK in cur or L_SIN in cur):
                m = re.search(L_NIJU + r"(.*?)" + R_NIJU, src, re.S)
                span = m.group(1).strip() if m else ""
                if len(span) >= 0.8 * max(len(src.strip()), 1) and cur.count(L_CUR) == 1:
                    cur = cur.replace(L_CUR, L_NIJU)
                    cur = cur.replace(R_CUR, R_NIJU)
                    tags.append("standalone->niju")

    # ---------- B. 「」 -> “” ----------
    if L_KAK in cur or R_KAK in cur:
        cur = cur.replace(L_KAK, L_CUR).replace(R_KAK, R_CUR)
        tags.append("kakko->curly")

    return cur, tags


# 语言对 -> 处理器。新增语言对只需在此注册，主流程无需改动。
HANDLERS = {
    ("ja", "zh-hans"): fix_ja_zh_hans,
}


def main() -> int:
    setup_io()
    ap = argparse.ArgumentParser(description="标点规范化（按语言对选择策略；未注册语言对跳过）")
    ap.add_argument("--source", metavar="PATH", help="源文档路径（默认按 _paths.py 优先级定位）")
    ap.add_argument("--apply", action="store_true", help="写回文件（默认 dry-run）")
    args = ap.parse_args()

    # ---- 语言对分派：未配置 / 未注册 -> 安全跳过，绝不猜测、不回退 ----
    src_lang, tgt_lang = lang_pair()
    if not src_lang or not tgt_lang:
        print("未配置源/目标语言（NOVEL_SOURCE_LANG / NOVEL_TARGET_LANG），跳过标点规范化")
        return 0
    handler = HANDLERS.get((src_lang, tgt_lang))
    if handler is None:
        print(f"未配置标点规范化策略：{src_lang} → {tgt_lang}，跳过")
        return 0
    print(f"标点规范化策略：{src_lang} → {tgt_lang}")

    mf = json.loads(read_text(MANIFEST_PATH))
    orig = read_text(ORIGINAL_PATH)
    state = json.loads(read_text(STATE_PATH))

    n_total = 0        # 含『』的原文段落
    n_nested = 0       # 『』 -> ‘’
    n_standalone = 0   # “”  -> 『』
    n_kakko = 0        # 「」 -> “”
    samples: list[tuple[str, str, str]] = []

    for prec in mf["parts"]:
        pid = prec["part_id"]
        opath = PARTS_OUT_DIR / f"{pid}.txt"
        lines = read_text(opath).split("\n")
        idxs = [i for i, l in enumerate(lines) if l.strip()]
        paras = prec["paragraphs"]
        if len(idxs) != len(paras):
            print(f"FATAL: {pid} 行数 {len(idxs)} != 段落数 {len(paras)}", file=sys.stderr)
            return 2

        for k, para in zip(idxs, paras):
            src = orig[para["abs_start_char"]:para["abs_end_char"]]
            tr = lines[k]
            cur, tags = handler(src, tr)

            for tag in tags:
                if tag == "has-niju":
                    n_total += 1
                    continue
                if tag == "nested->''":
                    n_nested += 1
                elif tag == "standalone->niju":
                    n_standalone += 1
                elif tag == "kakko->curly":
                    n_kakko += 1
                    if len(samples) >= 10:      # 与改造前一致：B 的样本最多 10 条
                        continue
                samples.append((tag, tr[:50], cur[:50]))

            lines[k] = cur

        if args.apply:
            data = ("\n".join(lines) + "\n").encode("utf-8")
            tmp = opath.with_name(opath.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(opath)
            state["parts"][pid]["output_sha256"] = sha256_bytes(opath.read_bytes())
            state["parts"][pid]["output_char_count"] = len(read_text(opath))

    print("=" * 72)
    print(f"含『』的原文段落        : {n_total}")
    print(f"  A1 嵌套 『』-> ‘’      : {n_nested}")
    print(f"  A3 独立 “” -> 『』      : {n_standalone}")
    print(f"  B  「」 -> “”           : {n_kakko} 段")
    print("=" * 72)
    for tag, old, new in samples[:6]:
        print(f"[{tag}]")
        print("  OLD:", old)
        print("  NEW:", new)

    if args.apply:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
        print("-" * 72)
        print("已写回 .translate/parts_out/ 并刷新 state.json 的 output_sha256")
        print("下一步：python scripts/merge.py")
    else:
        print("-" * 72)
        print("(dry-run，未写入任何文件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
