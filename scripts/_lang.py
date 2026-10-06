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

语言对
------
    lang_pair()         归一后的 (源语言, 目标语言)，供按语言对查找处理器使用
                        （如 fix_quotes.py 的 handler、verify.py 的残留检测器）
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


# 中文变体：简体与繁体的标点规范不同（繁体保留「」），因此归一成两个 key
ZH_HANS = {"zh", "zh-cn", "zh-hans", "zh-chs", "zh-hans-cn"}
ZH_HANT = {"zh-tw", "zh-hk", "zh-mo", "zh-hant", "zh-cht", "zh-hant-tw", "zh-hant-hk"}


def norm_lang(code: str) -> str:
    """
    把语言代码归一为「处理器查找 key」：
        ja / ja-JP        -> ja
        zh / zh-CN / zh-Hans -> zh-hans
        zh-TW / zh-Hant   -> zh-hant
        其它（en / ko / fr …）-> 主标签

    空串表示未配置。
    """
    c = (code or "").strip().lower().replace("_", "-")
    if not c:
        return ""
    if c in ZH_HANS:
        return "zh-hans"
    if c in ZH_HANT:
        return "zh-hant"
    return c.split("-")[0]


def lang_pair() -> tuple[str, str]:
    """归一后的 (源语言 key, 目标语言 key)；任一项为空串表示未配置"""
    return norm_lang(source_code()), norm_lang(target_code())
