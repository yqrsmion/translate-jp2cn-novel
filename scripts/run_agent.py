#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_agent.py —— 单 part 原子翻译流程

    state → manifest → 唯一指定 part → 输入 hash 校验 → 按需上下文
          → prompt → LLM → 净化 → 异常检测 → 逐段结构校验
          → 原子写 .translate/parts_out/part_XXX.txt → 更新 state → 推进下一个 part

程序负责：顺序、hash、完整性、状态、写盘、恢复、推进。
LLM 只负责：把给定的 part_text 完整译为目标语言（NOVEL_TARGET_LANG）。
LLM 不得判断下一 part、不得判断是否全书完成、不得输出任何元数据。

语言与体裁由环境变量决定：NOVEL_SOURCE_LANG / NOVEL_TARGET_LANG
（及可选的 *_LANG_NAME 覆盖）、NOVEL_GENRE（详见 _lang.py）。

运行模式
--------
    # 1) 自动 call LLM（需 NOVEL_LLM_BASE_URL / NOVEL_LLM_API_KEY / NOVEL_LLM_MODEL）
    python scripts/run_agent.py

    # 2) 打印 prompt，由外部 LLM 翻译（适用于 IDE 内的 Agent 自己担任翻译者）
    python scripts/run_agent.py --print-prompt

    # 3) 从 stdin 接收译文
    cat reply.txt | python scripts/run_agent.py --from-stdin

    # 4) 提交已写入 .translate/parts_out/part_XXX.txt 的译文（校验 + 原子写 + 更新 state）
    python scripts/run_agent.py --part part_001 --commit

单 part 推进；不传 --part 时严格取 manifest 顺序中的第一个非 DONE。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from _paths import (  # noqa: E402  ROOT 由源文档位置推导
    ROOT,
    SOURCE,
    ORIGINAL_PATH,
    MANIFEST_PATH,
    STATE_PATH,
    PARTS_OUT_DIR,
    INCOMING_DIR,
    WORK_DIR,
)
from _lang import (  # noqa: E402  语言 / 体裁配置（不依赖源文档）
    source_code,
    source_name,
    target_name,
    genre,
)

def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "")
    return int(v) if v.strip().isdigit() else default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name, "")
    try:
        return float(v)
    except ValueError:
        return default


MAX_ATTEMPTS = 3
CALL_TIMEOUT = _env_int("NOVEL_CALL_TIMEOUT", 900)        # 单次 LLM 调用超时（秒）
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
CONTEXT_BUDGET = _env_int("NOVEL_CONTEXT_BUDGET", 8000)   # work/ 注入上限（字符）
LLM_TEMPERATURE = _env_float("NOVEL_LLM_TEMPERATURE", 0.2)

PREV_TRANS_TAIL = _env_int("NOVEL_PREV_TRANS_TAIL", 1200)  # 前一片译文尾部
PREV_RAW_TAIL = _env_int("NOVEL_PREV_RAW_TAIL", 600)       # 前一片原文尾部

# --------------------------------------------------------------------------
# 提示词：主块（语言无关）+ 按需追加的附加块
# --------------------------------------------------------------------------
# 主块只描述「翻译行为契约」，不含任何特定语言的读音 / 排版 / 体裁规则；
# 那些规则放进下面的附加块，由语言配置与 NOVEL_GENRE 决定是否追加。

SYSTEM_PROMPT_HEAD = """你是一名专业的{src} → {tgt}小说译者。你的任务是把给定的【本 part {src}原文】完整翻译成{tgt}。

## 总则
- 忠实于原文：不改变事实、不改变人物关系、不改变语气、不改变信息量。
- {tgt}自然通顺，但因追求{tgt}自然而改变原意是被禁止的。
- 不总结、不删减、不省略、不擅自补充原文没有的信息。
- 不把后文信息提前写入前文。

## 一致性要求
- 严格沿用【术语表 / 人物表】中已确定的译名与称呼。
- 年龄差、上下级、亲疏关系要在{tgt}里体现出来，
  不同人物的说话方式必须有所区别。
- 线索、时间关系、地点关系必须忠实，不得为了通顺而调整。

## 输出契约（违反即视为失败）
1. **只输出译文正文**，不得出现任何解释、总结、翻译说明、进度说明、
   part 编号、段落编号、Markdown 标记、代码块围栏、标题符号。
2. **逐行对应**：原文每一行（每个独立段落）对应输出一行{tgt}。
   行数必须与原文完全一致，顺序完全一致。
3. 不要把多个原文行合并成一行，也不要把一行拆成多行。
4. 遇到 ＊、○、── 等场景分隔符或纯符号行，也必须输出对应的一行
   （可保留该符号，或按{tgt}习惯输出等价的分隔标记），**不得留空、不得省略**。
5. 输出行数错、出现编号、出现「以下是译文」等元话语，都会被程序判为失败。
"""

# 附加块：日语振假名（ルビ）—— 源语言为日语时才追加
RUBY_BLOCK = """
## 振假名（ルビ）残留 —— 必须特别注意
本电子书的振假名在抽取时被内联进了正文，形式为「汉字/词 + 紧跟的假名」，例如：
    繫つながった        -> 读作 つながった
    復ふく讐しゅう      -> 读作 復讐（ふくしゅう）
    有あり栖す川がわ有あり栖す -> 有栖川有栖
处理规则：
1. 假名只是读音标注，**不是正文**，不得把假名当作句子内容再译一遍；
2. 只翻译被标注的汉字/词本身，输出一次即可；
3. 严禁在译文中出现「重复字词」「汉字+同义假名并列」这类噪声；
4. 若无法确定哪个是正字，保留最自然的日语常见写法并按译文一次输出。
"""

# 附加块：语体分层（敬语 / 普通体）—— 源语言存在敬语体系时才追加
KEIGO_BLOCK = """
## 语体分层
- 敬语、普通体、口语等语体差异要在{tgt}里体现出来。
"""

# 附加块：体裁 —— 设置了 NOVEL_GENRE 时才追加
GENRE_BLOCK = """
## 体裁：{genre}
- 遵守{genre}小说的类型惯例。
- 不提前揭晓后文才公开的信息：例如推理 / 悬疑类的凶手身份与诡计手法，
  不猜测、不分析、不在译文中补充说明。
"""

USER_TEMPLATE = """{context_block}
【本 part 信息】
part_id: {part_id}
chapter_label: {chapter_label}
原文行数: {line_count}（你的译文必须正好这么多行，一行对一行）

【本 part {src}原文】
{part_text}

【再次确认】只输出译文正文，行数与上面原文完全一致，不要输出任何其它内容。"""


def system_prompt() -> str:
    """
    组装系统提示词：主块 + 按需追加的附加块。

    附加块的追加条件（见 _lang.py）：
      - 源语言为日语（NOVEL_SOURCE_LANG=ja*）      → 振假名（ルビ）条款
      - 源语言有敬语 / 语体分层（ja* / ko*）        → 语体分层条款
      - 设置了 NOVEL_GENRE                          → 体裁条款
    """
    src, tgt = source_name(), target_name()
    code = source_code()
    text = SYSTEM_PROMPT_HEAD.format(src=src, tgt=tgt)
    if code.startswith("ja"):
        text += RUBY_BLOCK
    if code.startswith(("ja", "ko")):
        text += KEIGO_BLOCK.format(tgt=tgt)
    g = genre()
    if g:
        text += GENRE_BLOCK.format(genre=g)
    return text


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


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def save_state(state: dict, by: str = "run_agent.py") -> None:
    state["updated_at"] = now_iso()
    state["updated_by"] = by
    write_bytes_atomic(STATE_PATH, (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def recompute_current(state: dict, manifest: dict) -> None:
    for p in manifest["parts"]:
        if state["parts"].get(p["part_id"], {}).get("status") != "DONE":
            state["current_part_id"] = p["part_id"]
            return
    state["current_part_id"] = None


def recompute_counters(state: dict) -> None:
    c = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0, "needs_review": 0}
    key = {"PENDING": "pending", "IN_PROGRESS": "in_progress", "DONE": "done",
           "FAILED": "failed", "NEEDS_REVIEW": "needs_review"}
    for p in state["parts"].values():
        c[key[p.get("status", "PENDING")]] += 1
    state["counters"] = c


def ensure_dirs() -> None:
    PARTS_OUT_DIR.mkdir(parents=True, exist_ok=True)
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# state / manifest
# --------------------------------------------------------------------------
def check_state(manifest: dict, state: dict) -> None:
    if state.get("source_sha256") != manifest["source"]["sha256"]:
        sys.exit("FATAL: state.source_sha256 与当前原文不一致")
    probe = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if state.get("manifest_sha256") != sha256_bytes(canonical_json_bytes(probe)):
        sys.exit("FATAL: state.manifest_sha256 与当前 manifest 不一致")
    if state.get("part_count") != manifest.get("part_count"):
        sys.exit("FATAL: part_count 不一致")


def select_part(manifest: dict, state: dict, want: str | None) -> dict:
    order = [p["part_id"] for p in manifest["parts"]]
    if want:
        if want not in order:
            sys.exit(f"FATAL: manifest 中不存在 {want}")
        # 允许重试同一 part；但禁止逆序/跳跃到未推进到的 part
        first_todo = next(pid for pid in order if state["parts"][pid]["status"] != "DONE")
        idx_todo, idx_want = order.index(first_todo), order.index(want)
        if idx_want > idx_todo:
            sys.exit(f"FATAL: 禁止跳跃推进。当前应先处理 {first_todo}，而不是 {want}")
        target = want
    else:
        target = next(pid for pid in order if state["parts"][pid]["status"] != "DONE")
    st = state["parts"][target].get("status")
    if st == "FAILED":
        sys.exit(f"FATAL: {target} 处于 FAILED，禁止继续，请人工处理")
    if st == "DONE":
        sys.exit(f"FATAL: {target} 已完成，禁止重复翻译")
    return next(p for p in manifest["parts"] if p["part_id"] == target)


def claim(state: dict, pid: str) -> str:
    token = uuid.uuid4().hex
    state["parts"][pid]["status"] = "IN_PROGRESS"
    state["parts"][pid]["started_at"] = now_iso()
    state["parts"][pid]["claim"] = {
        "token": token, "pid": os.getpid(), "heartbeat": str(time.time()),
    }
    save_state(state)
    # 复核 token 归属（软互斥）
    recheck = load_json(STATE_PATH)
    got = recheck["parts"][pid].get("claim", {}).get("token")
    if got != token:
        sys.exit(f"FATAL: {pid} 已被其它进程占用（claim token 不匹配），本次不执行")
    return token


def bump_attempts(state: dict, pid: str) -> None:
    state["parts"][pid]["attempts"] = state["parts"][pid].get("attempts", 0) + 1
    save_state(state)


def unclaim(state: dict, pid: str) -> None:
    state["parts"][pid]["claim"] = {"token": None, "pid": None, "heartbeat": None}


# --------------------------------------------------------------------------
# 上下文：work/ 按需检索（运行时内存索引，不产生任何额外文件）
# --------------------------------------------------------------------------
WORK_PRIORITY = ["characters.md", "terms.md", "locations.md",
                 "relationships.md", "timeline.md", "facts.md"]

# terms.md 表格的常见表头首列（语言无关：这些是表头而非词条，跳过）
TERM_HEADER_WORDS = {
    "原文", "原文词", "源语言", "源文", "术语", "词条", "外文", "外语", "日文",
    "source", "original", "term", "word",
}


def kanji_only(s: str) -> str:
    """
    把字符串投影为其「汉字序列」。
    原文里的人名/术语常带有内联振假名（如 樋ひ藤とう），字面匹配会失败，
    双方都做汉字投影后即可稳定命中。
    """
    return "".join(
        ch for ch in s
        if "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" or ch in "々〆〇"
    )


def load_work_sections() -> list[tuple[str, str, str]]:
    """返回 [(source_file, heading, body)]，heading 为空串表示文件头部内容。"""
    out = []
    for name in WORK_PRIORITY:
        p = WORK_DIR / name
        if not p.exists():
            continue
        lines = p.read_text(encoding="utf-8").split("\n")
        heading, buf = "", []
        for ln in lines:
            if ln.startswith("## "):
                if heading or buf:
                    out.append((name, heading, "\n".join(buf).strip()))
                heading = ln[3:].strip()
                buf = [ln]
            else:
                if ln.startswith("> ") and ln.lstrip("> ").strip().startswith("仅记录"):
                    continue
                buf.append(ln)
        if heading or buf:
            out.append((name, heading, "\n".join(buf).strip()))
    return out


def select_work_context(part_text: str, budget: int = CONTEXT_BUDGET) -> tuple[str, list[str]]:
    sections = load_work_sections()
    haystack = kanji_only(part_text)
    # 术语表的表格首列（源语言原词）也作为检索键
    term_rows: list[tuple[str, str]] = []
    for src, heading, body in sections:
        if src != "terms.md":
            continue
        for line in body.split("\n"):
            if line.startswith("|"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) < 2 or not cells[0]:
                    continue
                # 跳过表头行与 Markdown 分隔行（表头文案随语言而变，不能写死）
                if cells[0].lower() in TERM_HEADER_WORDS:
                    continue
                if set(cells) <= {"---", ":---", "---:", ":--", "--:"}:
                    continue
                term_rows.append((cells[0], cells[1]))

    hits: list[tuple[int, str, str, str]] = []
    for src, heading, body in sections:
        if not heading:
            continue
        keys = [heading, heading.split("（")[0].split("(")[0]]
        keys += [t[0] for t in term_rows if src == "terms.md"]
        keys = [k for k in keys if k]
        matched = any(k in haystack for k in keys) or any(k in part_text for k in keys)
        if matched:
            prio = WORK_PRIORITY.index(src) if src in WORK_PRIORITY else 99
            hits.append((prio, src, heading, body))

    hits.sort(key=lambda x: x[0])
    used, chosen = 0, []
    for _prio, src, heading, body in hits:
        size = len(body)
        if used + size > budget and chosen:
            continue
        used += size
        chosen.append(f"[{src}] {heading}\n{body}")
    hit_names = [f"{src}::{h}" for _p, src, h, _b in hits]

    # 漏检兜底：单独列出本 part 命中的术语词条（即使整节未命中，也保证译名一致）
    gloss = [f"{src_term} → {tgt_term}" for src_term, tgt_term in term_rows
             if src_term and (src_term in part_text or kanji_only(src_term) in haystack)]
    if gloss:
        body = "\n".join(gloss[:40])
        chosen.append(f"[terms.md 命中词条]\n{body}")
        hit_names.append("terms.md::命中词条")
    return ("\n\n".join(chosen), hit_names)


def build_context_block(manifest: dict, prec: dict, part_id: str) -> tuple[str, list[str]]:
    order = [p["part_id"] for p in manifest["parts"]]
    idx = order.index(part_id)
    chunks = []
    if idx > 0:
        prev = order[idx - 1]
        prev_out = PARTS_OUT_DIR / f"{prev}.txt"
        if prev_out.exists():
            tail = read_text(prev_out)[-PREV_TRANS_TAIL:]
            chunks.append(
                "【前一片译文尾部 · 仅供保持人名/术语/语气一致；禁止翻译它、禁止输出它】\n" + tail
            )
        prev_raw = ROOT / manifest["parts"][idx - 1]["file"]
        if prev_raw.exists():
            tail_raw = read_text(prev_raw)[-PREV_RAW_TAIL:]
            chunks.append("【前一片原文尾部 · 仅供理解场景连续性；不要翻译】\n" + tail_raw)
    work_ctx, hits = select_work_context(read_text(ROOT / prec["file"]))
    if work_ctx:
        chunks.append("【术语 / 人物 / 地点 / 时间线（本项目已确定译法，必须沿用）】\n" + work_ctx)
    return ("\n\n".join(chunks), hits)


# --------------------------------------------------------------------------
# LLM 调用（OpenAI 兼容 Chat Completions，仅标准库）
# --------------------------------------------------------------------------
def call_llm(system: str, user: str, max_tokens: int) -> tuple[str, dict]:
    base = os.environ.get("NOVEL_LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("NOVEL_LLM_API_KEY", "")
    model = os.environ.get("NOVEL_LLM_MODEL", "")
    if not (base and key and model):
        raise RuntimeError(
            "未配置 LLM 环境变量 NOVEL_LLM_BASE_URL / NOVEL_LLM_API_KEY / NOVEL_LLM_MODEL。"
            "可改用 `--print-prompt` 由外部 LLM 翻译，再 `--commit` 提交。"
        )
    url = base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": LLM_TEMPERATURE,
        "max_tokens": max_tokens,
    }
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            choice = (raw.get("choices") or [{}])[0]
            text = choice.get("message", {}).get("content", "") or ""
            meta = {"finish_reason": choice.get("finish_reason"), "usage": raw.get("usage")}
            return text, meta
        except urllib.error.HTTPError as e:
            if e.code in RETRY_STATUS:
                wait = (2 ** attempt) * 5 + random.uniform(0, 3)
                print(f"  [retry {attempt + 1}] HTTP {e.code}，等待 {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                last_err = e
                continue
            raise
        except Exception as e:  # 超时 / 连接错误
            wait = (2 ** attempt) * 5 + random.uniform(0, 3)
            print(f"  [retry {attempt + 1}] {type(e).__name__}: {e}，等待 {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)
            last_err = e
    raise RuntimeError(f"LLM 调用失败：{last_err}")


# --------------------------------------------------------------------------
# 译文净化
# --------------------------------------------------------------------------
FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$")
PREFIX_NOISE = re.compile(
    r"^\s*(好的|以下是|下面是|译文如下|翻译如下|这是译文|已翻译|希望对你有帮助|"
    r"Note:|Sure|Here is)[:：,]?\s*$"
)


def clean_translation(raw: str) -> str:
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for ln in lines:
        if FENCE_RE.match(ln):
            continue
        if PREFIX_NOISE.match(ln):
            continue
        out.append(ln)
    # 去首尾空行
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    # 归一化：译文每行一个单元，行间不留空行（空行结构由 merge.py 依 manifest 确定性重建）
    return "\n".join(l.strip() for l in out if l.strip())


# --------------------------------------------------------------------------
# 提交：校验 + 原子写 + 更新 state + 推进
# --------------------------------------------------------------------------
def commit(manifest: dict, state: dict, prec: dict, part_path: Path,
           translation: str, claim_ok: bool = True) -> int:
    sys.path.insert(0, str(SCRIPTS_DIR))
    import verify  # noqa: E402

    pid = prec["part_id"]
    part_text = read_text(part_path)
    bands = verify.load_bands(state)
    res = verify.verify_part_output(prec, part_text, translation, bands)

    print("=" * 72)
    print(f"{pid} 校验结果: {res['level']}")
    print(f"  units 源/译: {res['text_units']}  ratio={res['ratio']}")
    for i in res["issues"]:
        print(f"  [{i['level']}] {i['code']} {i['msg']}")
    print("=" * 72)

    sp = state["parts"][pid]
    if res["level"] == "FAIL":
        unclaim(state, pid)
        sp["status"] = "FAILED" if sp.get("attempts", 0) >= MAX_ATTEMPTS else "IN_PROGRESS"
        sp["verify"] = {"level": res["level"], "checks": res["checks"],
                        "warnings": [i["msg"] for i in res["issues"]]}
        sp["needs_human_review"] = True
        state.setdefault("history", []).append({
            "at": now_iso(), "part_id": pid, "from": "IN_PROGRESS",
            "to": sp["status"], "by": "run_agent.py", "note": "结构校验未通过",
        })
        recompute_counters(state)
        recompute_current(state, manifest)
        save_state(state)
        print(f"FAIL：{pid} 未写入。请修正译文后重跑该 part（attempts={sp['attempts']}）。")
        return 2

    # —— 原子写：先写 .incoming，再从磁盘重算 sha256，最后 replace ——
    ensure_dirs()
    data = (translation + "\n").encode("utf-8")
    staging = INCOMING_DIR / f"{pid}.txt"
    with open(staging, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    disk_sha = sha256_bytes(staging.read_bytes())
    final = PARTS_OUT_DIR / f"{pid}.txt"
    os.replace(staging, final)

    sp["status"] = "DONE"
    sp["finished_at"] = now_iso()
    sp["output_sha256"] = disk_sha
    sp["output_char_count"] = len(translation)
    sp["text_units"] = res["text_units"]
    sp["ratio"] = res["ratio"]
    sp["verify"] = {"level": res["level"], "checks": res["checks"],
                    "warnings": [i["msg"] for i in res["issues"] if i["level"] == "WARN"]}
    sp["needs_human_review"] = (res["level"] == "WARN")
    unclaim(state, pid)
    state.setdefault("history", []).append({
        "at": now_iso(), "part_id": pid, "from": "IN_PROGRESS", "to": "DONE",
        "by": "run_agent.py", "note": f"units {res['text_units']}",
    })
    recompute_counters(state)
    recompute_current(state, manifest)
    save_state(state)
    print(f"{pid} -> DONE（output {len(translation)} 字符，sha256 {disk_sha[:12]}…）")
    print(f"current_part_id = {state['current_part_id']}")
    return 0 if res["level"] == "PASS" else 1


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    setup_io()
    ap = argparse.ArgumentParser(description="单 part 原子翻译")
    ap.add_argument("--source", metavar="PATH", help="源文档路径（默认按 _paths.py 优先级定位）")
    ap.add_argument("--part", help="指定 part_id（默认取第一个非 DONE）")
    ap.add_argument("--print-prompt", action="store_true", help="打印组装好的 prompt 后退出")
    ap.add_argument("--from-stdin", action="store_true", help="从 stdin 读取译文")
    ap.add_argument("--commit", action="store_true", help="提交已有的 .translate/parts_out/part_XXX.txt")
    ap.add_argument("--max-tokens", type=int, default=0)
    args = ap.parse_args()

    if not MANIFEST_PATH.exists() or not STATE_PATH.exists():
        sys.exit("FATAL: 缺少 manifest.json 或 state.json")
    manifest = load_json(MANIFEST_PATH)
    state = load_json(STATE_PATH)
    check_state(manifest, state)
    ensure_dirs()

    # —— commit 模式：不翻译，只做「校验 → 原子归一化 → 更新 state」——
    if args.commit:
        prec = select_part(manifest, state, args.part)
        bump_attempts(state, prec["part_id"])
        opath = PARTS_OUT_DIR / f"{prec['part_id']}.txt"
        if not opath.exists():
            sys.exit(f"FATAL: 找不到待提交译文 {opath}")
        raw = read_text(opath)
        return commit(manifest, state, prec, ROOT / prec["file"], clean_translation(raw))

    prec = select_part(manifest, state, args.part)
    pid = prec["part_id"]
    part_path = ROOT / prec["file"]
    if not part_path.exists():
        sys.exit(f"FATAL: 缺少 part 文件 {part_path}")
    part_data = part_path.read_bytes()
    if sha256_bytes(part_data) != prec["sha256"]:
        sys.exit(f"FATAL: {pid} 内容与 manifest 记录的 sha256 不一致")
    part_text = part_data.decode("utf-8")

    context_block, hits = build_context_block(manifest, prec, pid)
    prompt = USER_TEMPLATE.format(
        context_block=context_block,
        part_id=pid,
        chapter_label=prec.get("chapter_label") or "-",
        line_count=prec["text_unit_count"],
        src=source_name(),
        part_text=part_text,
    )

    if args.from_stdin:
        raw = sys.stdin.read()
        bump_attempts(state, pid)
        token = claim(state, pid)
        return commit(manifest, state, prec, part_path, clean_translation(raw))

    if args.print_prompt:
        print(system_prompt())
        print("\n" + "=" * 72 + "\n")
        print(prompt)
        print(f"\n[INFO] {pid} 待译 {prec['text_unit_count']} 行 / {prec['char_count']} 字符；"
              f"work 命中：{hits if hits else '无'}")
        return 0

    max_tokens = args.max_tokens or int(
        min(max(prec["char_count"] * 1.6, 4096), 32000)
    )
    bump_attempts(state, pid)
    token = claim(state, pid)
    print(f"[{pid}] 开始翻译：{prec['char_count']} 字符 / {prec['text_unit_count']} 行，"
          f"max_tokens={max_tokens}")
    try:
        raw, meta = call_llm(system_prompt(), prompt, max_tokens)
    except Exception as e:
        unclaim(state, pid)
        sp = state["parts"][pid]
        sp["status"] = "FAILED" if sp.get("attempts", 0) >= MAX_ATTEMPTS else "PENDING"
        state.setdefault("history", []).append({
            "at": now_iso(), "part_id": pid, "from": "IN_PROGRESS",
            "to": sp["status"], "by": "run_agent.py", "note": f"LLM 调用失败: {e}",
        })
        recompute_counters(state)
        recompute_current(state, manifest)
        save_state(state)
        print(f"FATAL: LLM 调用失败：{e}", file=sys.stderr)
        return 2

    if meta.get("finish_reason") == "length":
        unclaim(state, pid)
        sp = state["parts"][pid]
        sp["status"] = "FAILED" if sp.get("attempts", 0) >= MAX_ATTEMPTS else "PENDING"
        save_state(state)
        print("FATAL: LLM 输出被 max_tokens 截断（禁止把截断输出当完整译文）", file=sys.stderr)
        return 2

    return commit(manifest, state, prec, part_path, clean_translation(raw))


if __name__ == "__main__":
    sys.exit(main())
