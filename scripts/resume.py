#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
resume.py —— 状态查看、完整性审计、崩溃恢复、人工确认后的重置

核心规则（对应 AGENTS.md 第二十条 / 第二十一条）
-----------------------------------------------
1. 进度推进完全由本工程的数据面（manifest + state）决定，LLM 不参与。
2. 严格执行 part_001 → part_002 → … 的线性顺序，禁止跳过 / 逆序 / 并行 / 重复。
3. 崩溃恢复的第一原则是：**绝不自动跳到下一个 part，也绝不自动判定已完成。**
   - IN_PROGRESS 且 output 尚存且能通过 V9–V12 → 只能由人工 `settle` 确认后置 DONE；
   - IN_PROGRESS 但 output 不存在、`.incoming/` 有残留 → 报告，由人工决定保留或丢弃；
   - IN_PROGRESS 且两者皆无 → 无产物发布，可安全重试同一 part（attempts+1）；
   - attempts >= 3 → 置 FAILED，停止推进，等待人工处理。
4. 全书完成的**唯一**判据：所有 part 均为 DONE 且 current_part_id == null。
   「正文出现结局」与「翻译任务完成」完全无关。

用法
----
    python scripts/resume.py init
    python scripts/resume.py status
    python scripts/resume.py next
    python scripts/resume.py audit
    python scripts/resume.py incoming [--discard part_XXX]
    python scripts/resume.py settle --part part_XXX --i-know
    python scripts/resume.py unlock --part part_XXX
    python scripts/resume.py reset  --part part_XXX --i-know --reason "..."
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from _paths import (  # noqa: E402  ROOT 由源文档位置推导
    ROOT,
    SOURCE,
    MANIFEST_PATH,
    STATE_PATH,
    TRANSLATE_DIR,
    PARTS_OUT_DIR,
    out_txt_path,
    INCOMING_DIR,
    ARCHIVE_DIR,
    WORK_DIR,
)

STATE_SCHEMA = "novel-translate-state"
STATE_SCHEMA_VERSION = "1.0"
MAX_ATTEMPTS = 3
STALE_HEARTBEAT_SEC = 30 * 60

WORK_FILES = {
    "characters.md": (
        "# 人物表\n\n"
        "> 仅记录原文已明确的信息。禁止写入推测（如“可能是犯人”“应在说谎”）。\n"
        "> 追加格式：以 `## 日文原名` 为小节标题。\n"
    ),
    "terms.md": (
        "# 术语表\n\n"
        "| 日文 | 中文 | 备注 |\n|---|---|---|\n"
    ),
    "locations.md": "# 地点与建筑\n\n",
    "relationships.md": "# 人物关系\n\n",
    "timeline.md": "# 时间线\n\n> 仅记录原文明确给出的时间信息。\n",
    "facts.md": "# 客观事实\n\n> 仅记录原文已确认的事实（物品出现位置、某人在场等）。\n",
    "conflicts.md": (
        "# 冲突登记\n\n"
        "> 发现与已有条目冲突时在此登记，**不自动覆盖**原条目，等待人工确认。\n"
        "> 记录字段：part_id / 原条目 / 新发现 / 冲突原因 / 建议处理方式。\n"
    ),
}

STATUS_ORDER = ["DONE", "IN_PROGRESS", "PENDING", "NEEDS_REVIEW", "FAILED"]
BLOCKING = {"PENDING", "IN_PROGRESS", "FAILED", "NEEDS_REVIEW"}


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
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


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


def save_state(state: dict) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_bytes_atomic(
        STATE_PATH, (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        sys.exit(f"FATAL: 找不到 {MANIFEST_PATH}，请先运行 split.py")
    return load_json(MANIFEST_PATH)


def load_state() -> dict:
    if not STATE_PATH.exists():
        sys.exit(f"FATAL: 找不到 {STATE_PATH}，请先运行 `resume.py init`")
    return load_json(STATE_PATH)


def check_binding(state: dict, manifest: dict) -> list[str]:
    errs = []
    if state.get("source_sha256") != manifest["source"]["sha256"]:
        errs.append("state.source_sha256 与 manifest 不一致（原文已被替换）")
    probe = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    calc = sha256_bytes(canonical_json_bytes(probe))
    if state.get("manifest_sha256") != calc:
        errs.append("state.manifest_sha256 与当前 manifest 不一致（manifest 已变更）")
    if state.get("part_count") != manifest.get("part_count"):
        errs.append("part_count 不一致")
    return errs


def ensure_work_files() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for name, header in WORK_FILES.items():
        p = WORK_DIR / name
        if not p.exists():
            p.write_text(header, encoding="utf-8")


def recompute_counters(state: dict) -> None:
    c = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0, "needs_review": 0}
    for p in state["parts"].values():
        st = p.get("status", "PENDING")
        key = {"PENDING": "pending", "IN_PROGRESS": "in_progress",
               "DONE": "done", "FAILED": "failed", "NEEDS_REVIEW": "needs_review"}[st]
        c[key] += 1
    state["counters"] = c


def recompute_current(state: dict, manifest: dict) -> None:
    """current_part_id = manifest 顺序中第一个非 DONE；全 DONE 时为 null。"""
    for p in manifest["parts"]:
        pid = p["part_id"]
        if state["parts"].get(pid, {}).get("status") != "DONE":
            state["current_part_id"] = pid
            return
    state["current_part_id"] = None


def stray_files() -> list[str]:
    found = []
    if INCOMING_DIR.exists():
        for f in sorted(INCOMING_DIR.iterdir()):
            found.append(f"incoming: {f.relative_to(ROOT).as_posix()} ({f.stat().st_size} B)")
    if PARTS_OUT_DIR.exists():
        for f in sorted(PARTS_OUT_DIR.iterdir()):
            if f.is_file() and f.name.endswith(".tmp"):
                found.append(f"stray tmp: {f.relative_to(ROOT).as_posix()}")
    out_path, _origin = out_txt_path()
    for probe in (TRANSLATE_DIR / "manifest.json.tmp", TRANSLATE_DIR / "state.json.tmp",
                  out_path.with_suffix(".txt.tmp")):
        if probe.exists():
            found.append(f"stray tmp: {probe.name}")
    for pat in ("gap_*", "debug_*", "temp_*", "test_*"):
        for f in ROOT.glob(pat):
            found.append(f"禁止文件: {f.name}")
    return found


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------
def cmd_init(args) -> int:
    manifest = load_manifest()
    if STATE_PATH.exists() and not args.rebuild:
        print(f"state.json 已存在。如需重建请显式加 --rebuild（不会删除任何译文）。")
        return 1
    probe = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    state = {
        "schema": STATE_SCHEMA,
        "schema_version": STATE_SCHEMA_VERSION,
        "source_sha256": manifest["source"]["sha256"],
        "manifest_sha256": sha256_bytes(canonical_json_bytes(probe)),
        "part_count": manifest["part_count"],
        "current_part_id": manifest["parts"][0]["part_id"],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "updated_by": "scripts/resume.py",
        "counters": {},
        "runtime": {"max_attempts": MAX_ATTEMPTS, "ratio_bands": None},
        "parts": {
            p["part_id"]: {
                "status": "PENDING",
                "attempts": 0,
                "started_at": None,
                "finished_at": None,
                "input_sha256": p["sha256"],
                "output_file": f".translate/parts_out/{p['part_id']}.txt",
                "output_sha256": None,
                "output_char_count": None,
                "text_units": None,
                "ratio": None,
                "verify": None,
                "needs_human_review": False,
                "claim": {"token": None, "pid": None, "heartbeat": None},
                "notes": None,
            }
            for p in manifest["parts"]
        },
        "history": [{
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "part_id": None, "from": None, "to": "INIT", "by": "resume.py",
            "note": f"bound to manifest {state_manifest_hint(manifest)}",
        }],
    }
    recompute_counters(state)
    save_state(state)
    ensure_work_files()
    print(f"state.json 已创建：{manifest['part_count']} 个 part，全部 PENDING")
    print(f"current_part_id = {state['current_part_id']}")
    print("翻译辅助上下文骨架已就绪：" + ", ".join(WORK_FILES))
    return 0


def state_manifest_hint(manifest: dict) -> str:
    return (manifest.get("manifest_sha256") or "")[:12]


# --------------------------------------------------------------------------
# status / next
# --------------------------------------------------------------------------
def cmd_status(args) -> int:
    manifest = load_manifest()
    state = load_state()
    errs = check_binding(state, manifest)
    if errs:
        print("[BINDING ERROR]")
        for e in errs:
            print("  " + e)
        return 2

    recompute_counters(state)
    c = state["counters"]
    total = manifest["part_count"]
    print("=" * 74)
    print(f"part: {c['done']}/{total} DONE   "
          f"PENDING={c['pending']}  IN_PROGRESS={c['in_progress']}  "
          f"NEEDS_REVIEW={c['needs_review']}  FAILED={c['failed']}")
    print(f"current_part_id = {state['current_part_id']}")
    print(f"updated_at      = {state.get('updated_at')}")
    print("-" * 74)
    print(f"{'part_id':<10}{'status':<14}{'att':<5}{'ratio':<8}{'verify':<8}{'out(bytes)':>12}")
    for p in manifest["parts"]:
        pid = p["part_id"]
        s = state["parts"].get(pid, {})
        op = PARTS_OUT_DIR / f"{pid}.txt"
        size = op.stat().st_size if op.exists() else 0
        ratio = s.get("ratio")
        v = (s.get("verify") or {}).get("level", "-")
        flag = " *" if s.get("needs_human_review") else ""
        print(f"{pid:<10}{s.get('status','PENDING'):<14}{s.get('attempts',0):<5}"
              f"{(f'{ratio:.3f}' if ratio else '-'):<8}{v:<8}{size:>12}{flag}")
    print("-" * 74)

    # 悬挂状态诊断
    hanging = [pid for pid, s in state["parts"].items() if s.get("status") == "IN_PROGRESS"]
    if hanging:
        print("[IN_PROGRESS 需处理]")
        for pid in hanging:
            s = state["parts"][pid]
            op = PARTS_OUT_DIR / f"{pid}.txt"
            inc = INCOMING_DIR / f"{pid}.txt"
            age = ""
            hb = s.get("claim", {}).get("heartbeat")
            if hb:
                try:
                    delta = time.time() - float(hb)
                    age = f" (heartbeat {int(delta)}s ago)"
                except (TypeError, ValueError):
                    pass
            if op.exists():
                print(f"  {pid}: 译文已落盘，请人工核对后 `resume.py settle --part {pid} --i-know`{age}")
            elif inc.exists():
                print(f"  {pid}: 仅发现暂存草稿 .translate/incoming/{pid}.txt；"
                      f"人工确认后可 `resume.py incoming --discard {pid}` 再重跑{age}")
            else:
                print(f"  {pid}: 无任何产物，可安全重跑该 part（attempts={s.get('attempts')}）{age}")
    failed = [pid for pid, s in state["parts"].items() if s.get("status") == "FAILED"]
    if failed:
        print("[FAILED 阻塞] " + ", ".join(failed) + " —— 需人工处理后方可继续，不会自动跳过")
    review = [pid for pid, s in state["parts"].items() if s.get("needs_human_review")]
    if review:
        print("[待人工复核] " + ", ".join(review))

    sf = stray_files()
    if sf:
        print("[遗留文件]")
        for x in sf:
            print("  " + x)
    print("=" * 74)
    return 0


def cmd_next(args) -> int:
    manifest = load_manifest()
    state = load_state()
    errs = check_binding(state, manifest)
    if errs:
        print("[BINDING ERROR] " + "; ".join(errs), file=sys.stderr)
        return 2
    for p in manifest["parts"]:
        pid = p["part_id"]
        st = state["parts"].get(pid, {}).get("status", "PENDING")
        if st == "FAILED":
            print(f"FATAL: {pid} 处于 FAILED，禁止推进，请先人工处理", file=sys.stderr)
            return 2
        if st != "DONE":
            print(pid)
            return 0
    print("ALL_DONE")
    return 3


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------
def cmd_audit(args) -> int:
    sys.path.insert(0, str(SCRIPTS_DIR))
    import verify  # noqa: E402

    mf = verify.load_manifest()
    rep = verify.Report()
    verify.verify_manifest_self(rep, mf)
    verify.verify_source(rep, mf)
    texts = verify.verify_parts_and_rebuild(rep, mf)

    state = load_state() if STATE_PATH.exists() else None
    bands = verify.load_bands(state)
    for p in mf["parts"]:
        pid = p["part_id"]
        if pid not in texts:
            continue
        opath = PARTS_OUT_DIR / f"{pid}.txt"
        if not opath.exists():
            continue
        _, out_text = verify.read_text_bytes(opath)
        res = verify.verify_part_output(p, texts[pid], out_text, bands)
        for issue in res["issues"]:
            rep.add(issue["level"], issue["code"], issue["msg"], pid)

    if state:
        binding = check_binding(state, mf)
        for e in binding:
            rep.add(verify.FAIL, "STATE", e)

    verify.print_report(rep)
    return verify._SEVERITY[rep.level]


# --------------------------------------------------------------------------
# settle / unlock / reset / incoming
# --------------------------------------------------------------------------
def cmd_settle(args) -> int:
    manifest = load_manifest()
    state = load_state()
    pid = args.part
    s = state["parts"].get(pid)
    if s is None:
        print(f"FATAL: state 中无 {pid}")
        return 2
    if s.get("status") not in ("IN_PROGRESS", "NEEDS_REVIEW"):
        print(f"INFO: {pid} 当前状态为 {s.get('status')}，无需 settle")
        return 0

    opath = PARTS_OUT_DIR / f"{pid}.txt"
    if not opath.exists():
        print(f"FATAL: {pid} 没有落盘译文，不能 settle（禁止凭空判定完成）")
        return 2

    if not args.i_know:
        print("需要先由人工核对译文，确认无误后加上 --i-know 才会置为 DONE。")
        return 1

    data = opath.read_bytes()
    out_text = data.decode("utf-8-sig")
    prec = next(p for p in manifest["parts"] if p["part_id"] == pid)
    part_text = (ROOT / prec["file"]).read_bytes().decode("utf-8-sig")

    sys.path.insert(0, str(SCRIPTS_DIR))
    import verify  # noqa: E402
    res = verify.verify_part_output(prec, part_text, out_text, verify.load_bands(state))
    if res["level"] == verify.FAIL:
        print(f"FATAL: {pid} 译文未通过结构校验，禁止 settle：")
        for i in res["issues"]:
            print(f"  [{i['level']}] {i['code']} {i['msg']}")
        return 2

    s["status"] = "DONE"
    s["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    s["output_sha256"] = sha256_bytes(data)
    s["output_char_count"] = len(out_text)
    s["text_units"] = res["text_units"]
    s["ratio"] = res["ratio"]
    s["verify"] = {"level": res["level"], "checks": res["checks"],
                   "warnings": [i["msg"] for i in res["issues"]]}
    s["needs_human_review"] = (res["level"] != "PASS")
    s["claim"] = {"token": None, "pid": None, "heartbeat": None}
    state.setdefault("history", []).append({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "part_id": pid,
        "from": "IN_PROGRESS", "to": "DONE", "by": "resume.py settle",
        "note": args.reason or "人工确认后 settle",
    })
    recompute_counters(state)
    recompute_current(state, manifest)
    save_state(state)
    print(f"{pid} -> DONE；current_part_id = {state['current_part_id']}")
    return 0


def cmd_unlock(args) -> int:
    state = load_state()
    s = state["parts"].get(args.part)
    if s is None:
        print(f"FATAL: state 中无 {args.part}")
        return 2
    if s.get("status") != "IN_PROGRESS":
        print(f"INFO: {args.part} 状态为 {s.get('status')}，无需解锁")
        return 0
    s["claim"] = {"token": None, "pid": None, "heartbeat": None}
    save_state(state)
    print(f"{args.part} 的 claim 已清除，可重新运行 run_agent.py 翻译该 part")
    return 0


def cmd_reset(args) -> int:
    manifest = load_manifest()
    state = load_state()
    s = state["parts"].get(args.part)
    if s is None:
        print(f"FATAL: state 中无 {args.part}")
        return 2
    if not args.i_know:
        print("reset 会把该 part 退回 PENDING 并把旧译文移入 .translate/archive/，请确认后加 --i-know")
        return 1
    opath = PARTS_OUT_DIR / f"{args.part}.txt"
    if opath.exists():
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        dst = ARCHIVE_DIR / f"{args.part}.txt.{ts}"
        shutil.move(str(opath), str(dst))
        print(f"旧译文已归档 -> {dst.relative_to(ROOT).as_posix()}")
    s.update({
        "status": "PENDING", "attempts": 0,
        "started_at": None, "finished_at": None,
        "output_sha256": None, "output_char_count": None,
        "text_units": None, "ratio": None, "verify": None,
        "needs_human_review": False,
        "claim": {"token": None, "pid": None, "heartbeat": None},
        "notes": args.reason or None,
    })
    state.setdefault("history", []).append({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "part_id": args.part,
        "from": "PREV", "to": "PENDING", "by": "resume.py reset",
        "note": args.reason or "",
    })
    recompute_counters(state)
    recompute_current(state, manifest)
    save_state(state)
    print(f"{args.part} -> PENDING；current_part_id = {state['current_part_id']}")
    return 0


def cmd_incoming(args) -> int:
    if not INCOMING_DIR.exists():
        print(".translate/incoming/ 不存在，无暂存草稿")
        return 0
    items = sorted(INCOMING_DIR.iterdir())
    if not items:
        print(".translate/incoming/ 为空")
        return 0
    print("暂存草稿：")
    for f in items:
        print(f"  {f.name}  {f.stat().st_size} B")
    if args.discard:
        target = INCOMING_DIR / f"{args.discard}.txt"
        if not target.exists():
            print(f"FATAL: 不存在 {target.name}")
            return 2
        if not args.i_know:
            print("请确认后加 --i-know")
            return 1
        target.unlink()
        print(f"已丢弃 {target.name}（重跑该 part 即可）")
    return 0


def main() -> int:
    setup_io()
    ap = argparse.ArgumentParser(description="进度查看 / 审计 / 恢复")
    # 注意：--source 是顶层参数，须写在子命令之前，如 resume.py --source X status
    ap.add_argument("--source", metavar="PATH", help="源文档路径（默认按 _paths.py 优先级定位）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化 state.json")
    p.add_argument("--rebuild", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="查看进度与悬挂状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("next", help="打印下一个应翻译的 part_id")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("audit", help="调用 verify 做全量审计")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("settle", help="人工确认后把悬挂 part 置为 DONE")
    p.add_argument("--part", required=True)
    p.add_argument("--i-know", action="store_true")
    p.add_argument("--reason")
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser("unlock", help="清除过期 claim")
    p.add_argument("--part", required=True)
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("reset", help="人工确认后重置某 part")
    p.add_argument("--part", required=True)
    p.add_argument("--i-know", action="store_true")
    p.add_argument("--reason")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("incoming", help="查看/处置 .incoming 暂存草稿")
    p.add_argument("--discard", metavar="PART_ID")
    p.add_argument("--i-know", action="store_true")
    p.set_defaults(func=cmd_incoming)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
