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
manifest.json state.json），最终产物是 ROOT/<中文书名>.txt（与源文档同一级）。
中文书名 = 源文件名（日文标题）的中文翻译，由 Agent 在合并时通过
`merge.py --title "中文书名"` 传入并写入 state.json 的 output.title_cn；
未取到中文书名时回退为 ROOT/<原名>_中文版.txt。
二者分离：.translate/ 是可恢复的工作状态，最终译文是交付物。

注意：中文书名要到合并阶段才存在，因此「最终产物路径」不能做成模块级常量，
必须由运行时函数 out_txt_path() 解析（见文件末尾）。

源文档定位优先级
----------------
1. 用户在提示词中显式指定的文档 —— 由 Agent 通过 --source 传入（最高优先级）
2. --source <路径> 命令行参数（可在任意位置，本模块直接扫描 sys.argv）
3. 环境变量 NOVEL_SOURCE
4. 当前工作目录下唯一的 *.txt
5. 存在多个 *.txt —— 报错并请用户指定，绝不自行猜测

用法（各脚本内）
----------------
    from _paths import ROOT, SOURCE
    from _paths import out_txt_path      # 最终产物路径（需运行时解析）

OUT_TXT_NAME / OUT_TXT_PATH 只是「未取到中文书名」时的回退值，
凡是要落盘或校验最终译文的地方，一律用 out_txt_path()。
"""

from __future__ import annotations

import json
import os
import re
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


# ---- 最终产物命名 ----
# 主规则：源 `日文标题.txt` -> `中文书名.txt`（中文书名由 Agent 翻译后经 merge.py --title 传入）
# 回退规则：源 `X.txt` -> `X_中文版.txt`（未取到中文书名时）
FALLBACK_SUFFIX = "_中文版"
TITLE_MAX_LEN = 80
_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def fallback_name(source: Path) -> str:
    """源 `X.txt` -> `X_中文版.txt`（未取到中文书名时的回退名）"""
    return f"{source.stem}{FALLBACK_SUFFIX}.txt"


def sanitize_title(raw: str) -> str | None:
    """
    把中文书名清洗成可安全用作文件名的主干（不含 .txt）。

    清洗步骤：去首尾空白 -> 剥离误带的 .txt 后缀 -> 去控制字符 ->
    非法字符替换为 _ -> 折叠连续空白 -> 去掉结尾的 . 与空格 -> 限长。
    结果为空时返回 None（交由调用方走回退命名）。
    """
    if not raw:
        return None
    name = raw.strip()
    if name.lower().endswith(".txt"):
        name = name[:-4].strip()
    name = _CONTROL_RE.sub("", name)
    name = _ILLEGAL_CHARS_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ").strip()
    if len(name) > TITLE_MAX_LEN:
        name = name[:TITLE_MAX_LEN].rstrip(". ").strip()
    return name or None


def read_title_cn() -> str | None:
    """
    读 state.json 中已持久化的中文书名（output.title_cn）。

    state.json 尚不存在（split / init 之前）或损坏时一律返回 None，
    不抛异常——命名解析不能阻塞切分、初始化、翻译等前置阶段。
    """
    try:
        if not STATE_PATH.exists():
            return None
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return None
    out = state.get("output") if isinstance(state, dict) else None
    if not isinstance(out, dict):
        return None
    return sanitize_title(out.get("title_cn") or "")


def out_txt_path() -> tuple[Path, str]:
    """
    运行时解析最终译文路径。

    返回 (路径, 来源)：来源为 "title"（用了中文书名）或 "fallback"（回退名）。
    中文书名与源文档同路径时强制回退——绝不允许产物覆盖源文档。
    """
    title = read_title_cn()
    if title:
        candidate = ROOT / f"{title}.txt"
        if candidate.resolve() != SOURCE.resolve():
            return candidate, "title"
    return ROOT / fallback_name(SOURCE), "fallback"


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
# 仅作回退值保留（中文书名尚未确定时的默认路径）；
# 需要真实产物路径的脚本请调用 out_txt_path()。
OUT_TXT_NAME = fallback_name(SOURCE)
OUT_TXT_PATH = ROOT / OUT_TXT_NAME


if __name__ == "__main__":
    resolved, origin = out_txt_path()
    print(f"ROOT       = {ROOT}")
    print(f"SOURCE     = {SOURCE}")
    print(f"WORKSPACE  = {TRANSLATE_DIR}")
    print(f"OUTPUT     = {resolved}  (name source: {origin})")
