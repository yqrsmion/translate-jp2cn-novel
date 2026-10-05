#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fix_quotes.py —— 统一引号排版（方案 B：中文规范优先）

规则（逐段依据「原文该处符号的角色」决定译文符号）
--------------------------------------------------
A. 『』 的处理
   1. 『』嵌套在「」之内（引用中的引用） -> 中文规范：内层单引号 ‘...’
   2. 『』为书名 / 作品名                -> 中文规范：书名号 《...》（已正确的不动）
   3. 『』独立成段（车内广播 / 公告 / 独立引用，全段无「」）
                                        -> 保留 『...』，以区别于人物对话
      仅当「整段就是一个 『...』」且译文只有一对 “...” 时才整段转 『』；
      长叙述段中嵌入的『』保留译文现有处理，避免误改同段并存的对话引号。

B. 全局：日文直角引号 「」 -> 中文弯引号 “”
   简体中文不使用「」，一律转 “”。嵌套情形 「…『…』…」 经 A 处理后
   自然成为 “…‘…’…”。

依据：manifest.json 提供「原文段落 <-> 译文行」的 1:1 对齐，逐段精确定位，不靠猜。

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


def main() -> int:
    setup_io()
    ap = argparse.ArgumentParser(description="统一引号排版（中文规范优先）")
    ap.add_argument("--source", metavar="PATH", help="源文档路径（默认按 _paths.py 优先级定位）")
    ap.add_argument("--apply", action="store_true", help="写回文件（默认 dry-run）")
    args = ap.parse_args()

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
            cur = tr

            # ---------- A. 『』 规则 ----------
            if L_NIJU in src:
                n_total += 1
                if L_KAK in src:
                    # 1) 嵌套引用 -> 中文内层单引号
                    if L_NIJU in cur:
                        cur = cur.replace(L_NIJU, L_SIN).replace(R_NIJU, R_SIN)
                        n_nested += 1
                        samples.append(("nested->''", tr[:50], cur[:50]))
                else:
                    # 2)(3) 独立引用
                    if not (L_NIJU in cur or L_BOOK in cur or L_SIN in cur):
                        m = re.search(L_NIJU + r"(.*?)" + R_NIJU, src, re.S)
                        span = m.group(1).strip() if m else ""
                        if len(span) >= 0.8 * max(len(src.strip()), 1) and cur.count(L_CUR) == 1:
                            cur = cur.replace(L_CUR, L_NIJU).replace(R_CUR, R_NIJU)
                            n_standalone += 1
                            samples.append(("standalone->niju", tr[:50], cur[:50]))

            # ---------- B. 「」 -> “” ----------
            if L_KAK in cur or R_KAK in cur:
                cur = cur.replace(L_KAK, L_CUR).replace(R_KAK, R_CUR)
                n_kakko += 1
                if len(samples) < 10:
                    samples.append(("kakko->curly", tr[:50], cur[:50]))

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
