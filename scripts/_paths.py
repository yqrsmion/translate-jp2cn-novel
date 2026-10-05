#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
_paths.py —— 源文档定位与项目根目录推导（scripts/ 下各脚本共用）

为什么需要它
------------
skill 场景下，脚本位于 skill 目录、小说位于用户工作目录，二者不同源。
因此 ROOT 不能取「脚本的上级目录」，必须由【源文档位置】推导：

    ROOT = 源文档所在目录

中间产物全部收进 ROOT/.translate/（parts/ parts_out/ work/ archive/ incoming/
manifest.json state.json），最终产物是 ROOT/<原名>_translate.txt（与源文档同一级）。
二者分离：.translate/ 是可恢复的工作状态，_translate.txt 是交付物。

源文档定位优先级
----------------
1. 用户在提示词中显式指定的文档 —— 由 Agent 通过 --source 传入（最高优先级）
2. --source <路径> 命令行参数（可在任意位置，本模块直接扫描 sys.argv）
3. 环境变量 NOVEL_SOURCE
4. 当前工作目录下唯一的 *.txt
5. 存在多个 *.txt —— 报错并请用户指定，绝不自行猜测

用法（各脚本内）
----------------
    from _paths import ROOT, SOURCE, OUT_TXT_NAME
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_SOURCE = "NOVEL_SOURCE"


def _scan_argv_source(argv: list[str]) -> str | None:
    """扫描 sys.argv 中的 --source <路径> 或 --source=<路径>。"""
    for i, a in enumerate(argv):
        if a == "--source" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--source="):
            return a.split("=", 1)[1]
    return None


def _txt_candidates(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.txt") if p.is_file())


def resolve(argv: list[str] | None = None, cwd: Path | None = None) -> tuple[Path, Path]:
    """返回 (ROOT, SOURCE)。定位失败时抛出 SystemExit 并给出明确指引。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    cwd = Path(cwd) if cwd else Path.cwd()

    # --help 时不做定位，返回占位值，避免帮助信息被"找不到源文档"打断
    if "-h" in argv or "--help" in argv:
        return cwd, cwd / "source.txt"

    raw = _scan_argv_source(argv) or os.environ.get(ENV_SOURCE)
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = cwd / p
        p = p.resolve()
        if not p.is_file():
            raise SystemExit(f"FATAL: 指定的源文档不存在：{p}")
        return p.parent, p

    cands = _txt_candidates(cwd)
    if len(cands) == 1:
        return cwd, cands[0].resolve()
    if not cands:
        raise SystemExit(
            "FATAL: 未能定位源文档。\n"
            "  请用 --source <路径> 指定，或在该目录下放置唯一的 *.txt，\n"
            f"  也可设置环境变量 {ENV_SOURCE}。\n"
            f"  当前目录：{cwd}"
        )
    raise SystemExit(
        "FATAL: 当前目录存在多个 *.txt，无法确定源文档，请显式指定 --source：\n  "
        + "\n  ".join(str(c) for c in cands)
    )


def translate_name(source: Path) -> str:
    """源 `X.txt` -> `X_translate.txt`（与源文档同一级）"""
    return f"{source.stem}_translate.txt"


ROOT, SOURCE = resolve()
ORIGINAL_PATH = SOURCE

# ---- Translation Workspace（中间产物，全部收进 .translate/）----
TRANSLATE_DIR = ROOT / ".translate"
PARTS_DIRNAME = ".translate/parts"          # 写入 manifest.parts[].file，相对 ROOT 解析
PARTS_DIR = TRANSLATE_DIR / "parts"
WORK_DIR = TRANSLATE_DIR / "work"
ARCHIVE_DIR = TRANSLATE_DIR / "archive"
INCOMING_DIR = TRANSLATE_DIR / "incoming"
PARTS_OUT_DIR = TRANSLATE_DIR / "parts_out"  # 逐 part 译文（仍是中间产物）
MANIFEST_PATH = TRANSLATE_DIR / "manifest.json"
STATE_PATH = TRANSLATE_DIR / "state.json"

# ---- 最终交付物：与源文档同一级 ----
OUT_TXT_NAME = translate_name(SOURCE)
OUT_TXT_PATH = ROOT / OUT_TXT_NAME


if __name__ == "__main__":
    print(f"ROOT       = {ROOT}")
    print(f"SOURCE     = {SOURCE}")
    print(f"WORKSPACE  = {TRANSLATE_DIR}")
    print(f"OUTPUT     = {OUT_TXT_PATH}")
