#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
_lang.py —— 语言 / 体裁配置（run_agent.py 与 resume.py 共用）

本模块**不 import _paths**，因此不依赖源文档是否存在，可单独测试。

语言
----
    NOVEL_SOURCE_LANG / NOVEL_TARGET_LANG         语言代码（ja / zh / en / ko / fr …）
    NOVEL_SOURCE_LANG_NAME / NOVEL_TARGET_LANG_NAME
                                                  自然名，优先级最高，覆盖内置映射

优先级：*_LANG_NAME > 内置映射表 > 代码原值（并打印一次提示）

体裁
----
    NOVEL_GENRE         体裁名（如 推理 / 悬疑 / 科幻）；为空时不追加体裁条款
"""

from __future__ import annotations

import os
import sys

LANG_NAMES = {
    "ja": "日语",
    "zh": "中文",
    "zh-cn": "简体中文",
    "zh-hans": "简体中文",
    "zh-tw": "繁体中文",
    "zh-hant": "繁体中文",
    "en": "英语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "ru": "俄语",
    "it": "意大利语",
    "pt": "葡萄牙语",
    "nl": "荷兰语",
    "pl": "波兰语",
    "th": "泰语",
    "vi": "越南语",
}


def _code(env_var: str) -> str:
    return (os.environ.get(env_var) or "").strip().lower()


def source_code() -> str:
    """源语言代码（NOVEL_SOURCE_LANG），未设置返回空串"""
    return _code("NOVEL_SOURCE_LANG")


def target_code() -> str:
    """目标语言代码（NOVEL_TARGET_LANG），未设置返回空串"""
    return _code("NOVEL_TARGET_LANG")


def _name(env_name_var: str, env_code_var: str, fallback: str) -> str:
    explicit = (os.environ.get(env_name_var) or "").strip()
    if explicit:
        return explicit
    code = _code(env_code_var)
    if not code:
        return fallback
    if code in LANG_NAMES:
        return LANG_NAMES[code]
    print(
        f"[WARN] 未知语言代码 {code!r}（{env_code_var}），prompt 中将直接使用该代码；"
        f"可设置 {env_name_var} 指定自然名。",
        file=sys.stderr,
    )
    return code


def source_name() -> str:
    """源语言自然名（供 prompt 使用）；无从判断时返回「源语言」"""
    return _name("NOVEL_SOURCE_LANG_NAME", "NOVEL_SOURCE_LANG", "源语言")


def target_name() -> str:
    """目标语言自然名（供 prompt 使用）；无从判断时返回「目标语言」"""
    return _name("NOVEL_TARGET_LANG_NAME", "NOVEL_TARGET_LANG", "目标语言")


def genre() -> str:
    """体裁（NOVEL_GENRE），未设置返回空串"""
    return (os.environ.get("NOVEL_GENRE") or "").strip()
