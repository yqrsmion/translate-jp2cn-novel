#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
split.py —— 一次性切分【源文档】-> parts/part_XXX.txt + manifest.json

源文档不写死文件名，由 --source <路径> 指定（定位规则详见 _paths.py）。
项目根目录 ROOT = 源文档所在目录，所有产物均落在 ROOT 下。

设计要点
--------
1. 源文档是唯一权威数据源，本脚本绝不修改它。
2. part 是源文档的**纯字符切片** part = T[start:end]，不做任何字符规整、
   不改换行（LF 保持 LF）。
3. 所有权规则：某行末尾的换行连续段归属于**前一个** part（owner = preceding）。
   因此 concat(part_texts) == original.txt 与 sum(char_count) == len(T) 无条件严格成立。
4. 段落识别规则（本项目实测，不依赖 split("\\n\\n")）：
   本文件中换行连续段长度只可能为 {2,4,6}，不存在长度为 1 的换行，
   因此**每个非空行就是一个完整段落**。泛化实现如下：
        units = 扫描全文，取所有「非空行」为单元，记录 (start, end, sep_newlines)
        sep_newlines = 该行之前的换行连续段长度（0 表示该行位于文首）
        blank_before = max(sep_newlines - 1, 0)
5. 边界强度（越大越强）：
        STRONG : blank_before >= 5   —— 部 / 场景时间头 / ＊ / 书名 / 作者 / 奥付
        MID    : blank_before >= 3   —— 次级分隔
        PARA   : blank_before >= 1   —— 普通段落（密度极高，必然命中）
6. 硬约束：单个 part 字符数 <= HARD_MAX(20000)。该约束由算法保证，切分结束后
   还会 assert 复核，verify.py 再查一遍。

用法
----
    python scripts/split.py --plan          # 干跑，只打印计划，不写任何文件
    python scripts/split.py                 # 正式切分（只允许运行一次）

translation-guide.md「切分规则」：切分脚本仅在项目初始化运行一次；如发现切分错误，
必须删除整个 parts/ 并重新运行本脚本。因此本脚本在 parts/ 或 manifest.json
已存在时会拒绝运行。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from bisect import bisect_right
from pathlib import Path

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
SCHEMA_NAME = "novel-translate-manifest"
SCHEMA_VERSION = "1.0"
GENERATOR_NAME = "scripts/split.py"
GENERATOR_VERSION = "1.0"

def _env_int(name: str, default: int) -> int:
    """允许用环境变量覆盖切分尺寸；默认值为本项目实测的平衡点。"""
    v = os.environ.get(name, "")
    return int(v) if v.strip().isdigit() else default


TARGET_MIN = _env_int("NOVEL_TARGET_MIN", 8000)   # 目标下界
TARGET_MAX = _env_int("NOVEL_TARGET_MAX", 15000)  # 目标上界（贪心窗口上界）
HARD_MAX = _env_int("NOVEL_HARD_MAX", 20000)      # ★ 绝对硬上限，任何 part 不得超过
MIN_PART = _env_int("NOVEL_MIN_PART", 2000)       # 过小尾块判定阈值

LVL_STRONG = 5
LVL_MID = 3
LVL_PARA = 1

# ROOT 由【源文档位置】推导，而非脚本位置（详见 _paths.py）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, SOURCE, ORIGINAL_PATH, PARTS_DIRNAME, MANIFEST_PATH  # noqa: E402

SENTENCE_END_RE = re.compile(r"[。．！？!?」』”)］】〉》…‥]")
PART_RE = re.compile(r"^第[一二三四五六七八九十]+部$")

# 句子兜底时需要一并跳过的收尾标点/空白
TRAIL_SKIP_CHARS = " \t\r\n\u3000「」『』（）()［］[]｛｝{}〈〉《》…‥、，,．.。!！?？"
SYMBOL_ONLY_RE = re.compile(r"^[\s＊*☆★※─—ー＝=・･+＋#＃~～〈〉《》「」『』\[\]()（）]+$")


# --------------------------------------------------------------------------
# 通用小工具（本脚本自包含，不依赖工程内其它脚本）
# --------------------------------------------------------------------------
def setup_io() -> None:
    """强制 UTF-8 输出，规避 Windows GBK 控制台抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_str(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def read_source(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    has_bom = data[:3] == b"\xef\xbb\xbf"
    text = data.decode("utf-8-sig")
    if text and text[0] == "\ufeff":
        text = text[1:]
    return data, text


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # Windows 下无法 fsync 目录，忽略


def canonical_json_bytes(obj: dict) -> bytes:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


# --------------------------------------------------------------------------
# 单元提取
# --------------------------------------------------------------------------
def extract_units(text: str):
    """
    返回 units 列表，每项：
        {"start", "end", "sep_newlines", "blank_before", "text}

    ★ 语义：sep_newlines 是**该行之前**的换行连续段长度（不是之后）。
      于是：file == concat(('\n' * sep_newlines) + text for unit) + 文末换行段
      第一个单元的 sep_newlines = 0；文末换行段不归属任何单元，
      由最后一个 part 的文本承载（manifest.tail_newlines）。
    blank_before = max(sep_newlines - 1, 0) 即该行之前的空行数，作为边界强度。
    """
    units = []
    pos = 0
    n = len(text)
    prev_run = 0
    for m in re.finditer(r"\n+", text):
        s, e = m.span()
        line = text[pos:s]
        if line.strip():
            units.append({
                "start": pos,
                "end": s,
                "sep_newlines": prev_run,
                "blank_before": max(prev_run - 1, 0),
                "text": line,
            })
        prev_run = e - s
        pos = e
    tail = text[pos:]
    if tail.strip():
        units.append({
            "start": pos,
            "end": n,
            "sep_newlines": prev_run,
            "blank_before": max(prev_run - 1, 0),
            "text": tail,
        })
    if not units:
        raise SystemExit("FATAL: 原文没有任何非空行")
    if units[0]["sep_newlines"] != 0:
        units[0]["sep_newlines"] = 0
        units[0]["blank_before"] = 0
    return units


# --------------------------------------------------------------------------
# 切分规划：两级贪心 + 三级兜底
# --------------------------------------------------------------------------
def _starts_by_level(units, level: int):
    return [u["start"] for u in units if u["blank_before"] >= level and u["start"] > 0]


def pick(start: int, starts: list[int], hi_cap: int, n: int):
    """
    在 (start, min(start+TARGET_MAX, n)] 窗口内取该强度最大的边界。
    返回 None 表示该强度在窗口内无可用边界。
    注意 hi 必须与 n 取 min，否则最后一个 part 会越过 EOF（实测踩过的坑）。
    """
    hi = min(start + hi_cap, n)
    i = bisect_right(starts, hi) - 1
    if i < 0:
        return None
    cand = starts[i]
    return cand if cand > start else None


def sentence_fallback(text: str, start: int, n: int):
    """
    句子级兜底：在 (start, start+HARD_MAX] 内最后一次出现的句末标点之后切。
    返回全局偏移或 None。
    """
    hi = min(start + HARD_MAX, n)
    window = text[start:hi]
    matches = list(SENTENCE_END_RE.finditer(window))
    if not matches:
        return None
    m = matches[-1]
    end = start + m.end()
    # 跳过标点后紧跟的空白/换行，让下一个 part 从干净的空白处开始
    while end < hi and text[end] in TRAIL_SKIP_CHARS:
        end += 1
    return end if end > start else None


def plan_parts(text: str, units):
    """
    返回 [(start, end, forced_cut), ...]，严格满足：
        parts[0][0] == 0
        parts[i][1] == parts[i+1][0]
        parts[-1][1] == len(text)
        end - start <= HARD_MAX
    """
    n = len(text)
    starts_strong = _starts_by_level(units, LVL_STRONG)
    starts_mid = _starts_by_level(units, LVL_MID)
    starts_para = _starts_by_level(units, LVL_PARA)
    all_start = _starts_by_level(units, 0) + [n]  # 文末始终是合法终点

    parts = []
    start = 0
    guard = 0
    while start < n:
        guard += 1
        if guard > n:  # 绝不应发生，防御死循环
            raise SystemExit("FATAL: plan_parts 陷入死循环")
        end = pick(start, starts_strong, TARGET_MAX, n)
        forced = False
        if end is None:
            end = pick(start, starts_mid, TARGET_MAX, n)
        if end is None:
            end = pick(start, starts_para, TARGET_MAX, n)
        if end is None:
            end = sentence_fallback(text, start, n)
        if end is None:
            end = pick(start, all_start, HARD_MAX, n)
        if end is None:
            end = min(start + HARD_MAX, n)
            forced = (end < n)  # 落在文末属于自然终点，不算强制切分
        if end <= start:
            raise SystemExit(f"FATAL: 切分未推进 start={start} end={end}")
        parts.append((start, end, forced))
        start = end

    normalize_small_parts(parts)
    return parts


def normalize_small_parts(parts) -> None:
    """
    消除碎片 part：反复把小于 MIN_PART 的块并入相邻块（合并后仍不得超过 TARGET_MAX）。
    既处理文末小块（贪心到文末时可能连续留下两个碎片），也处理内部碎片。
    """
    changed = True
    while changed and len(parts) >= 2:
        changed = False
        s, e, f = parts[-1]
        if (e - s) < MIN_PART:
            ps, _pe, pf = parts[-2]
            if (e - ps) <= TARGET_MAX:
                parts[-2] = (ps, e, pf or f)
                parts.pop()
                changed = True
                continue
        for i in range(1, len(parts) - 1):
            s, e, f = parts[i]
            if (e - s) >= MIN_PART:
                continue
            pa, _pb1, fa = parts[i - 1]
            _pa2, pb, fb = parts[i + 1]
            if (pb - pa) <= TARGET_MAX:
                parts[i - 1] = (pa, pb, fa or fb or f)
                parts.pop(i)
                changed = True
                break


# --------------------------------------------------------------------------
# 章/节标签
# --------------------------------------------------------------------------
def is_heading(unit) -> bool:
    """
    标题行判据（避免把场景首句误判为标题）：
      - 前面有 >=5 个空行（顶层分隔）
      - 长度 <= 40
      - 不是纯符号行
      - 不以全角空格开头（正文段落有缩进）
      - 不以 。/、 结尾（正文句子）
    """
    t = unit["text"].strip()
    if not t or len(t) > 40:
        return False
    if SYMBOL_ONLY_RE.match(t):
        return False
    if unit["text"].startswith("\u3000"):
        return False
    if t.endswith(("。", "、", "．")):
        return False
    return unit["blank_before"] >= LVL_STRONG


def build_labels(units, parts):
    """
    为每个 part 生成 chapter_label / first_heading / part_titles。
    chapter_label 形如：第一部 / 八月四日　十四時四十五分 + …
    """
    labels = []
    current_bu = ""
    unit_idx_to_part = {}
    for pi, (s, e, _f) in enumerate(parts):
        for ui, u in enumerate(units):
            if s <= u["start"] < e:
                unit_idx_to_part[ui] = pi

    carry_label = ""
    # 需要先遍历一次获得每个 part 内的标题与「部」归属
    per_part_titles = [[] for _ in parts]
    bu_at_part = ["" for _ in parts]
    bu = ""
    for ui, u in enumerate(units):
        t = u["text"].strip()
        if PART_RE.match(t):
            bu = t
        pi = unit_idx_to_part.get(ui)
        if pi is None:
            continue
        if bu_at_part[pi] == "":
            bu_at_part[pi] = bu
        if is_heading(u) and not PART_RE.match(t):
            per_part_titles[pi].append(t)

    for pi in range(len(parts)):
        titles = per_part_titles[pi]
        bu_name = bu_at_part[pi]
        if titles:
            head_str = " + ".join(titles[:3]) + (" …" if len(titles) > 3 else "")
            label = f"{bu_name} / {head_str}" if bu_name else head_str
            first_heading = titles[0]
        else:
            label = f"{bu_name} / 承前（{carry_label}）" if bu_name and carry_label else (
                bu_name if bu_name else (f"承前（{carry_label}）" if carry_label else "")
            )
            first_heading = ""
        if titles:
            carry_label = titles[0]
        labels.append((label, first_heading, titles))
    return labels


# --------------------------------------------------------------------------
# manifest 构建
# --------------------------------------------------------------------------
def build_manifest(text: str, units, parts, src_bytes: bytes) -> dict:
    n = len(text)
    has_bom = src_bytes[:3] == b"\xef\xbb\xbf"
    crlf = text.count("\r\n")
    line_ending = "CRLF" if crlf and crlf == text.count("\n") else ("LF" if "\n" in text else "NONE")
    if crlf and crlf != text.count("\n"):
        line_ending = "MIXED"

    labels = build_labels(units, parts)
    part_records = []
    total_char = 0
    total_byte = 0

    for idx, (s, e, forced) in enumerate(parts):
        part_id = f"part_{idx + 1:03d}"
        part_text = text[s:e]
        pb = part_text.encode("utf-8")

        # 归属于本 part 的尾部换行数
        tail_newlines = len(part_text) - len(part_text.rstrip("\n"))

        pu = [u for u in units if s <= u["start"] < e]
        paragraphs = []
        for j, u in enumerate(pu, start=1):
            ptext = text[u["start"]:u["end"]]
            paragraphs.append({
                "paragraph_id": f"{part_id}:p{j:03d}",
                "index": j,
                "start_char": u["start"] - s,      # part 内相对偏移
                "end_char": u["end"] - s,
                "abs_start_char": u["start"],      # 全局偏移（交叉校验用）
                "abs_end_char": u["end"],
                "char_count": u["end"] - u["start"],
                "sha256": sha256_str(ptext),
                "sep_newlines": u["sep_newlines"],
                "blank_before": u["blank_before"],
                "is_heading": is_heading(u),
            })

        label, first_heading, titles = labels[idx]
        part_records.append({
            "part_id": part_id,
            "index": idx + 1,
            "file": f"{PARTS_DIRNAME}/{part_id}.txt",
            "start_char": s,
            "end_char": e,
            "char_count": e - s,
            "byte_count": len(pb),
            "sha256": sha256_bytes(pb),
            "chapter_label": label,
            "first_heading": first_heading,
            "headings_in_part": titles,
            "text_unit_count": len(pu),
            "tail_newlines": tail_newlines,
            "forced_cut": forced,
            "paragraphs": paragraphs,
        })
        total_char += e - s
        total_byte += len(pb)

    manifest = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "generator": {
            "name": GENERATOR_NAME,
            "version": GENERATOR_VERSION,
            "generated_at": now_iso(),
        },
        "config": {
            "target_min": TARGET_MIN,
            "target_max": TARGET_MAX,
            "hard_max": HARD_MAX,
            "min_part": MIN_PART,
            "levels": {"STRONG": LVL_STRONG, "MID": LVL_MID, "PARA": LVL_PARA},
            "unit_rule": "paragraph == non-blank line",
        },
        "source": {
            "path": ORIGINAL_PATH.name,
            "encoding": "utf-8",
            "line_ending": line_ending,
            "has_bom": has_bom,
            "sha256": sha256_bytes(src_bytes),
            "char_count": len(text),
            "byte_count": len(src_bytes),
        },
        "part_count": len(part_records),
        "total_char_count": total_char,
        "total_byte_count": total_byte,
        "parts": part_records,
    }
    manifest["manifest_sha256"] = sha256_bytes(canonical_json_bytes(manifest))
    return manifest


def self_check(manifest: dict, text: str) -> list[str]:
    """切分后的自检（simulate verify 的核心项），返回错误列表。"""
    errs = []
    parts = manifest["parts"]
    n = len(text)

    if parts[0]["start_char"] != 0:
        errs.append("V4: 首个 part 起点不为 0")
    if parts[-1]["end_char"] != n:
        errs.append(f"V4: 末个 part 终点 {parts[-1]['end_char']} != {n}")

    prev_end = 0
    for p in parts:
        if p["start_char"] != prev_end:
            errs.append(f"V4: {p['part_id']} 连续性断裂 {p['start_char']} != {prev_end}")
        if p["end_char"] <= p["start_char"]:
            errs.append(f"V4: {p['part_id']} 空区间")
        if p["end_char"] - p["start_char"] > HARD_MAX:
            errs.append(f"V7: {p['part_id']} 超过硬上限 {p['char_count']} > {HARD_MAX}")
        prev_end = p["end_char"]

    total = sum(p["char_count"] for p in parts)
    if total != n:
        errs.append(f"V5: Σchar_count={total} != {n}")

    # V6 重建：严格按 manifest 顺序拼接
    rebuilt = "".join(text[p["start_char"]:p["end_char"]] for p in parts)
    if rebuilt != text:
        errs.append("V6: 重建结果与原文不一致")
    if sha256_str(rebuilt) != sha256_str(text):
        errs.append("V6: 重建结果 SHA256 不一致")

    return errs


def report(parts, units, text: str) -> None:
    sizes = [(e - s, f) for (s, e, f) in parts]
    pure = [sz for sz, _ in sizes]
    print("=" * 68)
    print("split plan report")
    print("=" * 68)
    print(f"source chars        : {len(text)}")
    print(f"units (paragraphs)  : {len(units)}")
    print(f"parts               : {len(parts)}")
    print(f"sum(char_count)     : {sum(pure)}")
    print(f"min / max part size : {min(pure)} / {max(pure)}")
    print(f"hard_max({HARD_MAX}) violations: {sum(1 for x in pure if x > HARD_MAX)}")
    in_band = sum(1 for x in pure if TARGET_MIN <= x <= TARGET_MAX)
    print(f"in target band      : {in_band}/{len(pure)}")
    forced = [i + 1 for i, (_s, _e, f) in enumerate(parts) if f]
    print(f"forced cuts         : {forced if forced else 'none'}")
    print("-" * 68)
    for i, (sz, f) in enumerate(sizes, start=1):
        flag = "FORCED" if f else ""
        mark = "!OVER-TARGET" if sz > TARGET_MAX else ("!UNDER-TARGET" if sz < TARGET_MIN else "")
        print(f"  part_{i:03d}  {sz:>7d}  {mark} {flag}")
    print("=" * 68)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    setup_io()
    ap = argparse.ArgumentParser(description="一次性切分源文档")
    ap.add_argument("--plan", action="store_true", help="干跑：只打印计划，不写任何文件")
    ap.add_argument("--source", metavar="PATH", help="源文档路径（默认按 _paths.py 优先级定位）")
    args = ap.parse_args()

    if not ORIGINAL_PATH.exists():
        print(f"FATAL: 找不到原文 {ORIGINAL_PATH}", file=sys.stderr)
        return 2

    parts_dir = ROOT / PARTS_DIRNAME
    if not args.plan:
        existing = []
        if parts_dir.exists():
            existing.append(str(parts_dir))
        if MANIFEST_PATH.exists():
            existing.append(str(MANIFEST_PATH))
        if existing:
            print(
                "FATAL: 已存在 " + " / ".join(existing) + "\n"
                "translation-guide.md「切分规则」：禁止重复/追加切分。\n"
                "若确需重做，请先人工删除 parts/ 与 manifest.json，再重新运行本脚本。",
                file=sys.stderr,
            )
            return 2

    src_bytes, text = read_source(ORIGINAL_PATH)
    units = extract_units(text)
    if units[-1]["end"] != len(text):
        pass  # 尾部换行不计入单元，由末 part 承载

    parts = plan_parts(text, units)
    report(parts, units, text)

    manifest = build_manifest(text, units, parts, src_bytes)
    errs = self_check(manifest, text)
    if errs:
        print("\n[SELF-CHECK FAILED]", file=sys.stderr)
        for e in errs:
            print("  " + e, file=sys.stderr)
        return 2
    print("[SELF-CHECK] V4 连续性 OK / V5 总长 OK / V6 重建字符级一致 OK / V7 硬上限 OK")

    if args.plan:
        print("\n(dry-run: 未写入任何文件)")
        return 0

    for p in manifest["parts"]:
        data = text[p["start_char"]:p["end_char"]].encode("utf-8")
        write_bytes_atomic(ROOT / p["file"], data)

    # manifest.json 以缩进可读形式写出；manifest_sha256 由「剔除该字段后的规范化 JSON」
    # （sort_keys + 紧凑分隔符）计算，verify.py 用同一函数复核即可。
    write_bytes_atomic(
        MANIFEST_PATH,
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )

    print(f"\n写入 {len(manifest['parts'])} 个 part 文件 -> {ROOT / PARTS_DIRNAME}")
    print(f"写入 manifest -> {MANIFEST_PATH}")
    print(f"manifest_sha256 = {manifest['manifest_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
