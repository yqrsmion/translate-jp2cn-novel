#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
export_md.py —— 把 output/ 各 part 译文导出为便于阅读的 Markdown（<原名>.zh.md）

输出名以源文档名为准（如源 断锁.txt -> 断锁.zh.md），由 _paths.py 推导。

为什么不会"误判哪一行是标题"
----------------------------
标题判定不靠"猜译文文本"，而是读 manifest.json 中每个段落的 is_heading —— 该字段
由 split.py 在【原文】上用确定性规则算出：
    前置空行 >= 5 且 长度 <= 40 且 非纯符号 且 不以 。/、/． 结尾 且 不以全角空格开头
因此"把正文误判成标题"或"漏掉真标题"这两类识别错误基本不会发生。

标题【层级】才是需要规则推断的地方
----------------------------------
manifest 只存 is_heading 布尔值、不存层级，所以层级必须由本脚本推断：

    #   书名（全书第一个标题，位于第一个「第X部」之前）
    ##  「第X部」/ 卷          —— 以及「解说」这类全书级大章节
    ### 章标题 / 场景时间头
    无   正文段落
        书名页元数据（作者、封面插画）、奥付元数据（著者、发行、◎ 等）—— 降为正文

用法
----
    python scripts/export_md.py --inspect     # 只打印标题结构，不写文件
    python scripts/export_md.py               # 导出 translated.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ROOT 由【源文档位置】推导，而非脚本位置（详见 _paths.py）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, SOURCE, OUT_MD_NAME  # noqa: E402

MANIFEST_PATH = ROOT / "manifest.json"
ORIGINAL_PATH = SOURCE
OUTPUT_DIR = ROOT / "output"
OUT_MD = ROOT / OUT_MD_NAME   # 输出名以源文档名为准

PART_RE = re.compile(r"^第[一二三四五六七八九十]+部$")
# 书名页 / 奥付 的元数据行：命中即降为正文，不当标题
META_RE = re.compile(r"(著者|作家|カバー装画|発行|発行者|定価|印刷|製本|◎|本電子書籍)")


def setup_io() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def read_text(p: Path) -> str:
    t = p.read_bytes().decode("utf-8-sig")
    return t[1:] if t and t[0] == "\ufeff" else t


def load_json(p: Path) -> dict:
    return json.loads(read_text(p))


def main() -> int:
    setup_io()
    ap = argparse.ArgumentParser(description="导出 Markdown")
    ap.add_argument("--source", metavar="PATH", help="源文档路径（默认按 _paths.py 优先级定位）")
    ap.add_argument("--inspect", action="store_true", help="只打印标题结构，不写文件")
    ap.add_argument("--out", default=str(OUT_MD), help="输出路径")
    args = ap.parse_args()

    mf = load_json(MANIFEST_PATH)
    original = read_text(ORIGINAL_PATH)

    blocks = []           # (level, translated_line)
    headings_seen = []    # (level, src_short, translated_line)

    title_emitted = False
    seen_bu = False
    after_kaisetsu = False

    for prec in mf["parts"]:
        pid = prec["part_id"]
        opath = OUTPUT_DIR / f"{pid}.txt"
        if not opath.exists():
            print(f"FATAL: 缺少译文 {opath}", file=sys.stderr)
            return 2
        lines = [ln.strip() for ln in read_text(opath).split("\n") if ln.strip()]
        paras = prec.get("paragraphs", [])
        if len(lines) != len(paras):
            print(f"FATAL: {pid} 行数 {len(lines)} != 段落数 {len(paras)}", file=sys.stderr)
            return 2

        for ln, para in zip(lines, paras):
            src = original[para["abs_start_char"]:para["abs_end_char"]]
            st = src.strip()

            if PART_RE.match(st):
                level = 2                       # 部 / 卷
                seen_bu = True
            elif st.startswith("解説"):
                level = 2                       # 解说：全书级大章节
                after_kaisetsu = True
            elif META_RE.search(st):
                level = 0                       # 元数据行 -> 正文
            elif after_kaisetsu:
                level = 0                       # 奥付区：不再产生标题
            elif para.get("is_heading"):
                if not seen_bu and not title_emitted:
                    level = 1                   # 书名
                    title_emitted = True
                elif not seen_bu:
                    level = 0                   # 书名页其余元数据（作者 / 封面插画）
                else:
                    level = 3                   # 章标题 / 场景时间头
            else:
                level = 0                       # 正文

            blocks.append((level, ln))
            if level:
                headings_seen.append((level, st[:36], ln[:36]))

    print("=" * 74)
    print("标题结构检测")
    print("=" * 74)
    for lv, src, zh in headings_seen:
        print(f"  {'#' * lv}  {src}    ->    {zh}")
    print("-" * 74)
    print(f"段落总数 {len(blocks)}；标题 {len(headings_seen)} 条"
          f"（# {sum(1 for l, _, _ in headings_seen if l == 1)} / "
          f"## {sum(1 for l, _, _ in headings_seen if l == 2)} / "
          f"### {sum(1 for l, _, _ in headings_seen if l == 3)}）")
    print("=" * 74)
    if args.inspect:
        return 0

    out = []
    for level, ln in blocks:
        out.append(("#" * level + " " + ln) if level else ln)
        out.append("")
    out_path = Path(args.out)
    out_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    print(f"已导出 -> {out_path}（{out_path.stat().st_size} 字节 / {len(blocks)} 段）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
