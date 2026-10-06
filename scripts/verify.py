#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify.py —— 完整性 / 一致性 / 逐段结构校验（V1–V12）

域着眼于「程序负责确定性校验，LLM 只负责翻译」：
  Python 完成第一层全部结构校验；LLM 只在人工触发（--with-llm）时做第二层
  语义复核，且**永远不能把 FAIL 降为 PASS**。

检查项
------
V1  原文 sha256 / char_count / byte_count / 编码 / 换行形态 vs manifest.source
V2  manifest 自完整性（manifest_sha256 重算）+ schema + part_id 连续性与 index 单调
V3  part 文件：存在、sha256、char_count、byte_count
V4  连续性：无 gap / 无 overlap，start/end 首尾对齐原文
V5  Σchar_count == 原文字符数，Σbyte_count == 原文字节数
V6  重建：按 manifest 顺序拼接 part → 与原文**字符级全等**（并二次比对 sha256）
V7  硬上限：任一 part > 20000 FAIL；> 15000 WARN；< 2000 WARN（碎片）
V8  段落元数据：逐 part 重算 units 与 manifest.paragraphs 逐条一致
V9  output 存在性 / 与 state 记录的 output_sha256 一致
V10 段落级逐段校验（数量/顺序/空译/异常压缩/重复）
V11 禁用标记（与语言无关）：围栏、编号、说明性语句、省略标记
    + 源语言残留检测（按 source_lang 注册检测器；未配置 / 未注册则跳过）
V12 特殊行（＊/标题/时间戳/单行对白）必须非空译文

退出码： 0 = PASS  1 = WARN（需人工复核后才可 merge）  2 = FAIL（阻断）
默认报告打印到 stdout，**不落地任何报告文件**（避免不可纳管产物）。

用法
----
    python scripts/verify.py --source
    python scripts/verify.py --part part_001 [--part part_002 ...]
    python scripts/verify.py --all
    python scripts/verify.py --all --json
    python scripts/verify.py --calibrate          # 依据已有译文校准 ratio 区间
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import statistics
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
from _lang import (  # noqa: E402  语言配置（不重新实现语言解析）
    norm_lang,
    source_code,
)

HARD_MAX = 20000
TARGET_MAX = 15000
MIN_PART = 2000

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_SEVERITY = {PASS: 0, WARN: 1, FAIL: 2}

DEFAULT_BANDS = {
    # 不同语言对的压缩比差异很大，以下是初版兜底区间；part_001 试译后应用 --calibrate 校准
    "unit_warn_low": 0.45,
    "unit_warn_high": 1.80,
    "unit_fail_low": 0.30,
    "unit_fail_high": 2.50,
    "part_warn_low": 0.60,   # translation-guide.md「单 part 原子翻译」要求的整 part 告警线
    "part_warn_high": 2.50,
}

# V11 禁用标记（出现在译文中即视为污染最终译文）
FORBIDDEN_PATTERNS = [
    (r"^```", "markdown 围栏"),
    (r"^\s*[-*]\s+\[[pP]\d+\]", "段落编号"),
    (r"\[p\d{3}\]", "段落编号"),
    (r"\[part_?\d+\]", "part 编号"),
    (r"^(以下是|下面是)?\s*(译文|翻译)[:：]", "说明性语句"),
    (r"（此处省略）|（以下略）|（中略）|\[未翻译\]|\[省略\]", "省略标记"),
    (r"^第\s*\d+\s*部分", "章节编号"),
    (r"^#{1,6}\s", "markdown 标题"),
]

KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
NON_TEXT_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)


# --------------------------------------------------------------------------
# V11 源语言残留检测：按源语言注册检测器
#   未配置语言 / 未注册语言 -> 不检测（不猜测、不套用其它语言的规则）
#   新增语言只需在此注册一个 detector(out_text) -> list[str]
# --------------------------------------------------------------------------
KANA_RESIDUE_MAX = 0.15


def detect_ja_residue(out_text: str) -> list[str]:
    """日文原文：译文里残留假名的比例过高，说明漏译（阈值 0.15）"""
    total_chars = max(len(re.sub(r"\s", "", out_text)), 1)
    ratio = len(KANA_RE.findall(out_text)) / total_chars
    if ratio > KANA_RESIDUE_MAX:
        return [f"[日语残留率过高] {ratio:.2%}"]
    return []


RESIDUE_DETECTORS = {
    "ja": detect_ja_residue,
}

_residue_notice_done = False


def residue_detector(lang_key: str):
    """取源语言对应的残留检测器；不存在则返回 None（并只提示一次）"""
    global _residue_notice_done
    detector = RESIDUE_DETECTORS.get(lang_key)
    if detector is None and not _residue_notice_done:
        _residue_notice_done = True
        why = "未配置源语言（NOVEL_SOURCE_LANG）" if not lang_key else f"{lang_key} 无对应检测器"
        print(f"[INFO] {why}，跳过源语言残留检测", file=sys.stderr)
    return detector


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------
def setup_io() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_str(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json_bytes(obj: dict) -> bytes:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def read_text_bytes(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    text = data.decode("utf-8-sig")
    if text and text[0] == "\ufeff":
        text = text[1:]
    return data, text


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


def extract_units(part_text: str):
    """
    与 split.py 完全一致的单元抽取：非空行 = 一个段落，
    sep_newlines = **该行之前**的换行连续段长度（第一个单元为 0）。
    """
    units = []
    pos = 0
    n = len(part_text)
    prev_run = 0
    for m in re.finditer(r"\n+", part_text):
        s, e = m.span()
        line = part_text[pos:s]
        if line.strip():
            units.append({
                "start": pos, "end": s,
                "sep_newlines": prev_run,
                "blank_before": max(prev_run - 1, 0),
                "text": line,
            })
        prev_run = e - s
        pos = e
    tail = part_text[pos:]
    if tail.strip():
        units.append({
            "start": pos, "end": n,
            "sep_newlines": prev_run,
            "blank_before": max(prev_run - 1, 0),
            "text": tail,
        })
    if units:
        units[0]["sep_newlines"] = 0
        units[0]["blank_before"] = 0
    return units


class Report:
    def __init__(self) -> None:
        self.issues: list[dict] = []
        self.checks: list[str] = []

    def add(self, level: str, code: str, msg: str, part_id: str = "-") -> None:
        self.issues.append({"level": level, "code": code, "part_id": part_id, "msg": msg})

    def ok(self, code: str) -> None:
        self.checks.append(code)

    @property
    def level(self) -> str:
        if not self.issues:
            return PASS
        return max((i["level"] for i in self.issues), key=lambda lv: _SEVERITY[lv])

    def summary(self) -> dict:
        counts = {PASS: 0, WARN: 0, FAIL: 0}
        for i in self.issues:
            counts[i["level"]] += 1
        return {"level": self.level, "counts": counts}


# --------------------------------------------------------------------------
# V1–V8：源 / manifest / parts
# --------------------------------------------------------------------------
def verify_source(rep: Report, manifest: dict) -> None:
    if not ORIGINAL_PATH.exists():
        rep.add(FAIL, "V1", "源文档不存在")
        return
    data, text = read_text_bytes(ORIGINAL_PATH)
    src = manifest.get("source", {})

    if sha256_bytes(data) != src.get("sha256"):
        rep.add(FAIL, "V1", "original.txt SHA256 与 manifest 不一致（数据源已被替换）")
    else:
        rep.ok("V1.sha256")
    if len(data) != src.get("byte_count"):
        rep.add(FAIL, "V1", f"原文字节数 {len(data)} != manifest {src.get('byte_count')}")
    else:
        rep.ok("V1.byte_count")
    if len(text) != src.get("char_count"):
        rep.add(FAIL, "V1", f"原文字符数 {len(text)} != manifest {src.get('char_count')}")
    else:
        rep.ok("V1.char_count")

    has_bom = data[:3] == b"\xef\xbb\xbf"
    if bool(has_bom) != bool(src.get("has_bom")):
        rep.add(WARN, "V1", f"BOM 形态变化 {has_bom} vs {src.get('has_bom')}")
    crlf = text.count("\r\n")
    le = "CRLF" if crlf and crlf == text.count("\n") else ("LF" if "\n" in text else "NONE")
    if crlf and crlf != text.count("\n"):
        le = "MIXED"
    if le != src.get("line_ending"):
        rep.add(WARN, "V1", f"换行形态 {le} != manifest {src.get('line_ending')}")
    else:
        rep.ok("V1.line_ending")


def verify_manifest_self(rep: Report, manifest: dict) -> None:
    if manifest.get("schema") != "novel-translate-manifest":
        rep.add(FAIL, "V2", f"未知 schema {manifest.get('schema')}")
    if manifest.get("schema_version") not in ("1.0",):
        rep.add(FAIL, "V2", f"不支持的 schema_version {manifest.get('schema_version')}")

    stored = manifest.get("manifest_sha256")
    if not stored:
        rep.add(FAIL, "V2", "manifest 缺少 manifest_sha256")
        return
    probe = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    calc = sha256_bytes(canonical_json_bytes(probe))
    if calc != stored:
        rep.add(FAIL, "V2", f"manifest_sha256 不匹配（{calc[:12]}… != {stored[:12]}…）")
    else:
        rep.ok("V2.manifest_sha256")

    parts = manifest.get("parts", [])
    if manifest.get("part_count") != len(parts):
        rep.add(FAIL, "V2", f"part_count {manifest.get('part_count')} != len(parts) {len(parts)}")
    for i, p in enumerate(parts, start=1):
        expect_id = f"part_{i:03d}"
        if p.get("part_id") != expect_id:
            rep.add(FAIL, "V2", f"part_id 顺序异常：期望 {expect_id}，实际 {p.get('part_id')}")
            return
        if p.get("index") != i:
            rep.add(FAIL, "V2", f"{p.get('part_id')} index={p.get('index')} != {i}")
    rep.ok("V2.part_ids")

    if manifest.get("total_char_count") != sum(p.get("char_count", 0) for p in parts):
        rep.add(FAIL, "V2", "total_char_count 与 parts 之和不一致")
    if manifest.get("total_byte_count") != sum(p.get("byte_count", 0) for p in parts):
        rep.add(FAIL, "V2", "total_byte_count 与 parts 之和不一致")


def verify_parts_and_rebuild(rep: Report, manifest: dict) -> dict:
    """V3 / V4 / V5 / V6 / V7 / V8。返回 {part_id: part_text}"""
    src = manifest.get("source", {})
    n_expect = src.get("char_count")
    if not ORIGINAL_PATH.exists():
        return {}
    _, full_text = read_text_bytes(ORIGINAL_PATH)

    parts = manifest["parts"]
    texts: dict[str, str] = {}
    missing = False

    for p in parts:
        pid = p["part_id"]
        path = ROOT / p["file"]
        if not path.exists():
            rep.add(FAIL, "V3", f"{pid} 文件不存在：{p['file']}", pid)
            missing = True
            continue
        data, text = read_text_bytes(path)
        if sha256_bytes(data) != p.get("sha256"):
            rep.add(FAIL, "V3", f"{pid} SHA256 不一致", pid)
        if len(data) != p.get("byte_count"):
            rep.add(FAIL, "V3", f"{pid} 字节数 {len(data)} != {p.get('byte_count')}", pid)
        if len(text) != p.get("char_count"):
            rep.add(FAIL, "V3", f"{pid} 字符数 {len(text)} != {p.get('char_count')}", pid)
        texts[pid] = text

        # V7 硬上限
        size = p.get("char_count", 0)
        if size > HARD_MAX:
            rep.add(FAIL, "V7", f"{pid} 超过绝对硬上限：{size} > {HARD_MAX}", pid)
        elif size > TARGET_MAX:
            rep.add(WARN, "V7", f"{pid} 超过目标上界：{size} > {TARGET_MAX}", pid)
        elif size < MIN_PART:
            rep.add(WARN, "V7", f"{pid} 过小（碎片）：{size} < {MIN_PART}", pid)

        # V8 段落元数据复核
        check_paragraph_meta(rep, p, text)

    for p in parts:
        pid = p["part_id"]
        if pid not in texts:
            continue
        # V8.first-sep：首段落的前置换行数必须用全局切片校验（part 文件内无从得知）
        pre = full_text[:p["start_char"]]
        lead = len(pre) - len(pre.rstrip("\n"))
        paras = p.get("paragraphs") or []
        if paras and paras[0].get("sep_newlines") != lead:
            rep.add(FAIL, "V8",
                    f"{pid} 首段落前置换行 {paras[0].get('sep_newlines')} != 全局 {lead}", pid)
        expect_slice = full_text[p["start_char"]:p["end_char"]]
        if texts[pid] != expect_slice:
            rep.add(FAIL, "V8", f"{pid} 内容与 manifest 偏移切片不一致", pid)

    # V4 连续性
    prev_end = 0
    for p in parts:
        if p["start_char"] != prev_end:
            rep.add(FAIL, "V4", f"{p['part_id']} 断裂：start={p['start_char']} != 前一个 end={prev_end}", p["part_id"])
        if p["end_char"] <= p["start_char"]:
            rep.add(FAIL, "V4", f"{p['part_id']} 空区间", p["part_id"])
        prev_end = p["end_char"]
    if parts:
        if parts[0]["start_char"] != 0:
            rep.add(FAIL, "V4", "首个 part 起点不为 0", parts[0]["part_id"])
        if parts[-1]["end_char"] != n_expect:
            rep.add(FAIL, "V4", f"末个 part 终点 {parts[-1]['end_char']} != 原文字符数 {n_expect}", parts[-1]["part_id"])
    if not any(i["code"] == "V4" for i in rep.issues):
        rep.ok("V4.continuity")

    # V5 总长
    if parts:
        tc = sum(p.get("char_count", 0) for p in parts)
        tb = sum(p.get("byte_count", 0) for p in parts)
        if tc != n_expect:
            rep.add(FAIL, "V5", f"Σchar_count={tc} != 原文字符数 {n_expect}")
        else:
            rep.ok("V5.char")
        if tb != src.get("byte_count"):
            rep.add(FAIL, "V5", f"Σbyte_count={tb} != 原文字节数 {src.get('byte_count')}")
        else:
            rep.ok("V5.byte")

    # V6 重建（字符级）
    if missing:
        rep.add(FAIL, "V6", "存在缺失 part，无法重建")
        return texts
    rebuilt = "".join(texts[p["part_id"]] for p in parts)
    if rebuilt != full_text:
        rep.add(FAIL, "V6", "重建结果与原文不一致（字符级）")
    elif sha256_str(rebuilt) != sha256_str(full_text):
        rep.add(FAIL, "V6", "重建结果 SHA256 不一致")
    else:
        rep.ok("V6.reconstruct")
    del rebuilt
    return texts


def check_paragraph_meta(rep: Report, p: dict, part_text: str) -> None:
    """V8：part 内段落元数据与 manifest.paragraphs 逐条比对。"""
    units = extract_units(part_text)
    paras = p.get("paragraphs", [])
    if len(units) != len(paras):
        rep.add(FAIL, "V8", f"{p['part_id']} 段落数 {len(units)} != manifest {len(paras)}", p["part_id"])
        return
    if p.get("text_unit_count") != len(units):
        rep.add(FAIL, "V8", f"{p['part_id']} text_unit_count 不一致", p["part_id"])
    bad = 0
    for i, (u, para) in enumerate(zip(units, paras)):
        if (u["start"], u["end"], u["end"] - u["start"]) != (
            para.get("start_char"), para.get("end_char"), para.get("char_count")
        ):
            bad += 1
            continue
        if sha256_str(u["text"]) != para.get("sha256"):
            bad += 1
            continue
        # 首段落的 sep_newlines 属于「全局前置换行」，片内无法重算，
        # 由 verify_parts_and_rebuild 用全局切片单独校验（见 V8.first-sep）。
        if i == 0:
            continue
        if u["sep_newlines"] != para.get("sep_newlines"):
            bad += 1
    if bad:
        rep.add(FAIL, "V8", f"{p['part_id']} 有 {bad} 条段落元数据不一致", p["part_id"])


# --------------------------------------------------------------------------
# V9–V12：译文检查
# --------------------------------------------------------------------------
def align_units(src_units, out_units):
    """
    单调容错对齐：返回[(si, oi), ...]，缺失一侧为 None。
    数量相等时直接 1:1（最常见、最可靠）。
    """
    if len(src_units) == len(out_units):
        return [(i, i) for i in range(len(src_units))]
    pairs: list[tuple] = []
    sm = difflib.SequenceMatcher(a=src_units, b=out_units, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                pairs.append((i1 + k, j1 + k))
        elif tag == "delete":
            for k in range(i1, i2):
                pairs.append((k, None))
        elif tag == "insert":
            for k in range(j1, j2):
                pairs.append((None, k))
        else:  # replace：按 min 长度弱配对，其余记 unmatched
            pairs.extend((i1 + k, j1 + k) for k in range(min(i2 - i1, j2 - j1)))
            pairs.extend((k, None) for k in range(i1 + min(i2 - i1, j2 - j1), i2))
            pairs.extend((None, k) for k in range(j1 + min(i2 - i1, j2 - j1), j2))
    return pairs


def verify_part_output(part_rec: dict, part_text: str, out_text: str, bands: dict) -> dict:
    """
    单个 part 的 V9–V12 校验。返回结果 dict：
        {level, checks{}, issues[], text_units{}, ratio, detail{}}
    供 verify.py CLI 与 run_agent.py 共用。
    """
    result = {
        "level": PASS, "checks": {}, "issues": [], "text_units": {},
        "ratio": None, "detail": {},
    }

    def add(level: str, code: str, msg: str) -> None:
        result["issues"].append({"level": level, "code": code, "msg": msg})

    src_units = [u["text"].strip() for u in extract_units(part_text)]
    out_units = [ln.strip() for ln in out_text.split("\n") if ln.strip()]

    # ---- V9 基本存在性 ----
    if not out_text.strip():
        add(FAIL, "V9", "译文为空")
        result["level"] = FAIL
        result["checks"] = {k: FAIL for k in ("count", "order", "empty", "ratio", "duplicate", "forbidden")}
        return result
    result["checks"]["exists"] = PASS

    n_src, n_out = len(src_units), len(out_units)
    result["text_units"] = {"source": n_src, "translation": n_out, "delta": n_out - n_src}

    # ---- V11 禁用标记（与语言无关）----
    forb = []
    for pat, desc in FORBIDDEN_PATTERNS:
        for ln in out_units:
            if re.search(pat, ln):
                forb.append(f"[{desc}] {ln[:40]}")
                break

    # ---- V11 源语言残留检测（按 source_lang 注册；未配置 / 未注册 -> 跳过）----
    detector = residue_detector(norm_lang(source_code()))
    if detector is not None:
        forb.extend(detector(out_text))

    if forb:
        add(FAIL, "V11", "；".join(forb[:5]))
        result["checks"]["forbidden"] = FAIL
    else:
        result["checks"]["forbidden"] = PASS

    # ---- V10.A 数量 ----
    pairs = align_units(src_units, out_units)
    unmatched_src = sum(1 for si, _ in pairs if si is not None and _is_missing(pairs, si))
    if n_src == n_out:
        result["checks"]["count"] = PASS
    else:
        delta = abs(n_out - n_src) / max(n_src, 1)
        lvl = FAIL if delta > 0.10 else WARN
        add(lvl, "V10.A", f"段落数不匹配：源 {n_src} / 译 {n_out}（偏差 {delta:.1%}）")
        result["checks"]["count"] = lvl

    # ---- V10.B 顺序 ----
    seq = [oi for si, oi in pairs if si is not None and oi is not None]
    jumps = []
    for a, b in zip(seq, seq[1:]):
        if b is not None and a is not None and b < a:
            jumps.append((a, b))
    if jumps:
        add(FAIL, "V10.B", f"段落顺序出现 {len(jumps)} 处逆序（如 {jumps[0]}）")
        result["checks"]["order"] = FAIL
    else:
        result["checks"]["order"] = PASS

    # ---- V10.C 空译 / V10.D 压缩率 / V12 特殊行 ----
    empty_bad, ratio_bad, empty_lvl = [], [], PASS
    ratios = []
    for si, oi in pairs:
        if si is None or oi is None:
            continue
        s, t = src_units[si], out_units[oi]
        slen, tlen = len(s), len(t)
        if slen >= 15 and tlen <= 2:
            empty_bad.append(f"p{si + 1:03d}: {s[:20]}… -> “{t}”")
            empty_lvl = FAIL
        elif slen >= 15 and tlen < max(2, int(slen * 0.15)):
            empty_bad.append(f"p{si + 1:03d}: 异常短译 {slen}->{tlen}")
            empty_lvl = FAIL if empty_lvl != FAIL else FAIL
        if slen >= 8:
            r = tlen / slen
            ratios.append(r)
            if r < bands["unit_fail_low"] or r > bands["unit_fail_high"]:
                ratio_bad.append(f"p{si + 1:03d}: ratio={r:.2f}")
            elif r < bands["unit_warn_low"] or r > bands["unit_warn_high"]:
                ratio_bad.append(f"p{si + 1:03d}: ratio={r:.2f}(warn)")

    missing_notes = []
    for si, oi in pairs:
        if si is not None and oi is None:
            missing_notes.append(f"p{si + 1:03d}: {src_units[si][:24]}…")
            empty_lvl = FAIL
    if missing_notes:
        empty_bad.extend(missing_notes[:10])

    if empty_bad:
        add(empty_lvl, "V10.C", f"空译/疑似漏译 {len(empty_bad)} 处：" + "；".join(empty_bad[:5]))
        result["checks"]["empty"] = empty_lvl
    else:
        result["checks"]["empty"] = PASS

    if ratio_bad:
        # 存在任一落在 FAIL 区间（标记中不含 "(warn)"）的段落时整体判 FAIL；
        # 全部仅落在 WARN 区间（标记含 "(warn)"）时整体判 WARN。
        ratio_lvl = FAIL if any("(warn)" not in x for x in ratio_bad) else WARN
        add(ratio_lvl, "V10.D", f"段落字符比例异常 {len(ratio_bad)} 处：" + "；".join(ratio_bad[:4]))
        result["checks"]["ratio"] = ratio_lvl
    else:
        result["checks"]["ratio"] = PASS

    # ---- V10.E 重复 ----
    dup = []
    for i in range(1, len(out_units)):
        if out_units[i] == out_units[i - 1] and len(out_units[i]) > 8:
            dup.append(f"相邻重复 p{i:03d}/{i + 1:03d}")
    if n_out > 0:
        joined = "".join(out_units)
        shingle = 30
        seen, repeated = set(), 0
        cnt = 0
        for i in range(0, max(len(joined) - shingle, 0), shingle):
            piece = joined[i:i + shingle]
            cnt += 1
            if piece in seen:
                repeated += 1
            seen.add(piece)
        rep_rate = repeated / max(cnt, 1)
        if rep_rate > 0.15:
            dup.append(f"30 字 shingle 重复率 {rep_rate:.1%}")
    if dup:
        add(FAIL, "V10.E", "；".join(dup[:5]))
        result["checks"]["duplicate"] = FAIL
    else:
        result["checks"]["duplicate"] = PASS

    # ---- V12 特殊行 ----
    special_bad = []
    for si, oi in pairs:
        if si is None:
            continue
        s = src_units[si]
        if len(s) <= 3 or NON_TEXT_RE.match(s):
            if oi is None or not out_units[oi]:
                special_bad.append(f"p{si + 1:03d}: “{s}” 无对应译文")
    if special_bad:
        add(FAIL, "V12", "；".join(special_bad[:5]))
        result["checks"]["special"] = FAIL
    else:
        result["checks"]["special"] = PASS

    # ---- 整 part 比例（仅告警，translation-guide.md「单 part 原子翻译」）----
    src_len = sum(len(s) for s in src_units)
    out_len = sum(len(t) for t in out_units)
    ratio = (out_len / src_len) if src_len else 0.0
    result["ratio"] = round(ratio, 4)
    if ratio < bands["part_warn_low"] or ratio > bands["part_warn_high"]:
        add(WARN, "V10.part-ratio",
            f"整 part 字符比例 {ratio:.3f} 超出告警区 "
            f"[{bands['part_warn_low']}, {bands['part_warn_high']}]（仅告警，不作为完整性证明）")

    if ratios:
        result["detail"]["unit_ratio_median"] = round(statistics.median(ratios), 4)

    if result["issues"]:
        result["level"] = max((i["level"] for i in result["issues"]), key=lambda lv: _SEVERITY[lv])
    return result


def _is_missing(pairs, si: int) -> bool:
    for s_idx, o_idx in pairs:
        if s_idx == si and o_idx is None:
            return True
    return False


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        print(f"FATAL: 找不到 {MANIFEST_PATH}", file=sys.stderr)
        sys.exit(2)
    return load_json(MANIFEST_PATH)


def load_bands(state: dict | None) -> dict:
    bands = dict(DEFAULT_BANDS)
    if state:
        stored = (state.get("runtime") or {}).get("ratio_bands")
        if isinstance(stored, dict):
            for k in DEFAULT_BANDS:
                if k in stored:
                    bands[k] = stored[k]
    return bands


def print_report(rep: Report, extra: dict | None = None) -> None:
    level = rep.level
    print("=" * 72)
    print(f"verify result: {level}")
    print("=" * 72)
    if rep.checks:
        print("passed checks: " + ", ".join(sorted(set(rep.checks))))
    if not rep.issues:
        print("no issues.")
    else:
        order = {FAIL: 0, WARN: 1, PASS: 2}
        for i in sorted(rep.issues, key=lambda x: (order[x["level"]], x["code"], x["part_id"])):
            print(f"  [{i['level']}] {i['code']} {i['part_id']}: {i['msg']}")
    if extra:
        print("-" * 72)
        for k, v in extra.items():
            print(f"  {k}: {v}")
    print("=" * 72)


def cmd_source(args) -> int:
    mf = load_manifest()
    rep = Report()
    verify_manifest_self(rep, mf)
    verify_source(rep, mf)
    texts = verify_parts_and_rebuild(rep, mf)

    size_info = {}
    if texts:
        sizes = [len(t) for t in texts.values()]
        size_info = {
            "parts": len(sizes), "sum": sum(sizes),
            "min": min(sizes), "max": max(sizes),
            "over_hard_max": sum(1 for s in sizes if s > HARD_MAX),
            "over_target": sum(1 for s in sizes if s > TARGET_MAX),
        }
    if args.json:
        print(json.dumps({"level": rep.level, "issues": rep.issues, "size": size_info},
                         ensure_ascii=False, indent=2))
    else:
        print_report(rep, size_info)
    return _SEVERITY[rep.level]


def cmd_part(args) -> int:
    mf = load_manifest()
    state = load_json(STATE_PATH) if STATE_PATH.exists() else None
    bands = load_bands(state)

    targets = args.part
    part_of = {p["part_id"]: p for p in mf["parts"]}
    if not targets:
        targets = [p["part_id"] for p in mf["parts"]]

    rep = Report()
    details = {}
    for pid in targets:
        prec = part_of.get(pid)
        if prec is None:
            rep.add(FAIL, "V9", f"manifest 中不存在 {pid}", pid)
            continue
        ppath = ROOT / prec["file"]
        if not ppath.exists():
            rep.add(FAIL, "V3", "原文 part 缺失", pid)
            continue
        _, part_text = read_text_bytes(ppath)
        opath = PARTS_OUT_DIR / f"{pid}.txt"
        if not opath.exists():
            rep.add(WARN, "V9", f"尚无译文 {opath.name}", pid)
            continue
        data, out_text = read_text_bytes(opath)
        res = verify_part_output(prec, part_text, out_text, bands)
        details[pid] = res
        # V9 sha 与 state 记录一致性
        sp = (state or {}).get("parts", {}).get(pid, {})
        if sp.get("output_sha256") and sp["output_sha256"] != sha256_bytes(data):
            rep.add(FAIL, "V9", "output_sha256 与 state 记录不一致", pid)
        for issue in res["issues"]:
            rep.add(issue["level"], issue["code"], issue["msg"], pid)

    if args.write_state and STATE_PATH.exists():
        st = load_json(STATE_PATH)
        for pid, res in details.items():
            if pid in st.get("parts", {}):
                st["parts"][pid]["verify"] = {
                    "level": res["level"], "checks": res["checks"],
                    "warnings": [i["msg"] for i in res["issues"] if i["level"] == WARN],
                }
                st["parts"][pid]["text_units"] = res["text_units"]
                st["parts"][pid]["ratio"] = res["ratio"]
                st["parts"][pid]["needs_human_review"] = (res["level"] != PASS)
        write_bytes_atomic(
            STATE_PATH,
            (json.dumps(st, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    if args.json:
        print(json.dumps({"level": rep.level, "issues": rep.issues, "parts": details},
                         ensure_ascii=False, indent=2))
    else:
        extra = {}
        for pid, res in details.items():
            extra[pid] = (f"{res['level']} ratio={res['ratio']} "
                          f"units={res['text_units']}")
        print_report(rep, extra)
    return _SEVERITY[rep.level]


def cmd_all(args) -> int:
    mf = load_manifest()
    rep = Report()
    verify_manifest_self(rep, mf)
    verify_source(rep, mf)
    texts = verify_parts_and_rebuild(rep, mf)

    state = load_json(STATE_PATH) if STATE_PATH.exists() else None
    bands = load_bands(state)
    for p in mf["parts"]:
        pid = p["part_id"]
        if pid not in texts:
            continue
        opath = PARTS_OUT_DIR / f"{pid}.txt"
        if not opath.exists():
            continue
        _, out_text = read_text_bytes(opath)
        res = verify_part_output(p, texts[pid], out_text, bands)
        for issue in res["issues"]:
            rep.add(issue["level"], issue["code"], issue["msg"], pid)
    print_report(rep)
    if args.json:
        print(json.dumps(rep.issues, ensure_ascii=False, indent=2))
    return _SEVERITY[rep.level]


def cmd_calibrate(args) -> int:
    """依据已有译文重算合理的 unit ratio 告警区间（中位数 ± 3×MAD）。"""
    mf = load_manifest()
    ratios = []
    for p in mf["parts"]:
        ppath = ROOT / p["file"]
        opath = PARTS_OUT_DIR / f"{p['part_id']}.txt"
        if not (ppath.exists() and opath.exists()):
            continue
        _, ptext = read_text_bytes(ppath)
        _, otext = read_text_bytes(opath)
        su = [u["text"].strip() for u in extract_units(ptext)]
        ou = [l.strip() for l in otext.split("\n") if l.strip()]
        if len(su) != len(ou):
            continue
        for s, t in zip(su, ou):
            if len(s) >= 8:
                ratios.append(len(t) / len(s))
    if len(ratios) < 30:
        print(f"样本不足（{len(ratios)}），至少需要 30 条配对段落；当前沿用默认区间。")
        return 1
    ratios.sort()
    med = statistics.median(ratios)

    def pct(q: float) -> float:
        i = int(round((len(ratios) - 1) * q))
        return ratios[max(0, min(i, len(ratios) - 1))]

    p1, p99 = pct(0.01), pct(0.99)
    # 只可能放宽、绝不收紧默认区间，避免对极短段落产生误报
    bands = {
        "unit_warn_low": round(min(p1, DEFAULT_BANDS["unit_warn_low"]), 3),
        "unit_warn_high": round(max(p99, DEFAULT_BANDS["unit_warn_high"]), 3),
        "unit_fail_low": round(min(p1 * 0.8, DEFAULT_BANDS["unit_fail_low"]), 3),
        "unit_fail_high": round(max(p99 * 1.3, DEFAULT_BANDS["unit_fail_high"]), 3),
        "part_warn_low": DEFAULT_BANDS["part_warn_low"],
        "part_warn_high": DEFAULT_BANDS["part_warn_high"],
    }
    print(f"samples={len(ratios)} median={med:.3f} p1={p1:.3f} p99={p99:.3f}")
    print(json.dumps(bands, ensure_ascii=False, indent=2))
    if STATE_PATH.exists() and args.write_state:
        st = load_json(STATE_PATH)
        st.setdefault("runtime", {})["ratio_bands"] = bands
        write_bytes_atomic(
            STATE_PATH, (json.dumps(st, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )
        print("已写入 state.runtime.ratio_bands")
    return 0


def main() -> int:
    setup_io()
    ap = argparse.ArgumentParser(description="完整性与逐段结构校验")
    ap.add_argument("--source", metavar="PATH", help="源文档路径（默认按 _paths.py 优先级定位）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--source-only", action="store_true", help="只校验 原文/manifest/parts（V1–V8）")
    g.add_argument("--part", nargs="*", metavar="PART_ID", help="校验指定 part 的译文（V9–V12）")
    g.add_argument("--all", action="store_true", help="全部校验")
    g.add_argument("--calibrate", action="store_true", help="校准 ratio 告警区间")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--write-state", action="store_true", help="把校验结果写回 state.json")
    args = ap.parse_args()

    if args.source_only:
        return cmd_source(args)
    if args.all:
        return cmd_all(args)
    if args.calibrate:
        return cmd_calibrate(args)
    return cmd_part(args)


if __name__ == "__main__":
    sys.exit(main())
