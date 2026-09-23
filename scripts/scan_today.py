#!/usr/bin/env python3
"""decision-pulse: one day of Claude Code + Codex transcripts -> cognitive-load dashboard.

Usage:
  python scan_today.py [--date YYYY-MM-DD] [--html PATH] [--json]

  --date       day to scan (local time), default today
  --html PATH  write the dashboard page; prints only a one-line summary
  --json       print the full JSON (default when --html is not given)

Design contract: references/spec.md (goals G1–G11). Personal settings go in
config.local.json next to SKILL.md (see config.example.json); nothing
person-specific is hardcoded here. SKILL.md lists the field-semantics traps this
file has already fallen into — read it before trusting any new field.
"""
import argparse
import glob
import html
import json
import os
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_LOCAL = os.path.join(SKILL_DIR, "config.local.json")
# 每天的結果存這裡（個人資料，不進版控）：decision-pulse-<日期>.html、<日期>.json、published.json（日期 → 發佈連結）
HISTORY_DIR = os.path.join(SKILL_DIR, "history")
PUBLISHED = os.path.join(HISTORY_DIR, "published.json")

# Defaults every user gets. Personal values (e.g. high-risk topics) are empty on
# purpose: the tool must produce a complete page for someone who configures nothing.
DEFAULTS = {
    "timezone": None,  # None = this computer's timezone; or "+08:00"
    "high_risk_keywords": {},
    "irreversible_patterns": ["git push", "rm -rf", "drop table", "--force"],
    "time_segments": [
        {"name": "早", "start": 6, "end": 12},
        {"name": "中", "start": 12, "end": 18},
        {"name": "晚", "start": 18, "end": 22},
        {"name": "深夜", "start": 22, "end": 6, "late_night": True},
    ],
    "fatigue_block_min": 60,
    "long_output_chars": 400,
    "fast_accept_sec": 5,
    # [low→mid, mid→high] boundaries per stage. Experience values except where
    # SKILL.md says otherwise; each stage is graded on its own, never summed.
    "risk_levels": {
        "density": [25, 45],        # your messages per active hour
        "recovery": [60, 120],      # longest stretch without a 10-min break (min)
        "fatigue": [6, 12],         # conversation switches per active hour
        "judgement": [0.10, 0.25],  # share of long outputs answered within fast_accept_sec
    },
    "claude_projects": "~/.claude/projects",
    "codex_sessions": "~/.codex/sessions",
    "generic_dirnames": ["scripts", "src", "scratchpad", "app", "docs", "lib"],
}


# Role presets change only what is job-dependent: what counts as a normal
# message rate, which topics are high-stakes, which commands are irreversible,
# and the example in the advice. Recovery (10 min / 6 h), switching and judgement
# thresholds never move with the role — a job title doesn't change what tires a
# brain, and loosening them would make the tool explain overwork away.
ROLE_PRESETS = {
    "通用": {},
    "工程": {
        "density": [40, 70],
        "high_risk_keywords": ["production", "上線", "deploy", "migration", "資料庫", "權限", "secret"],
        "irreversible_extra": ["git reset --hard", "kubectl delete", "terraform apply", "migrate"],
    },
    "營運・HR": {
        "high_risk_keywords": ["調薪", "薪資", "離職", "資遣", "錄取", "申訴", "合約", "簽署", "撤權", "個資"],
        "density_example": "這類詢問先照範本回，特殊的再來問我",
        "switching_is_the_job": True,
    },
    "業務": {
        "density": [20, 40],
        "high_risk_keywords": ["報價", "折扣", "合約", "簽約", "退款", "付款條件"],
        "density_example": "報價照價目表出，要給折扣再問我",
    },
    "設計": {
        "density": [20, 40],
        "high_risk_keywords": ["上線", "對外發布", "品牌"],
        "density_example": "沿用品牌規範的色票和字級，不用每次確認",
    },
    "主管": {
        "density": [15, 30],
        "high_risk_keywords": ["預算", "人事", "績效", "考核", "組織調整", "合約"],
        "density_example": "例行報表照上週的格式，不用每次問我",
        "switching_is_the_job": True,
    },
}
ROLE_ALIASES = {"其他": "通用", "營運": "營運・HR", "HR": "營運・HR", "營運/HR": "營運・HR"}


def cli_role():
    """--role is read here, before argparse, because the whole config (and the
    module-level constants derived from it) is built at import time."""
    if "--role" in sys.argv:
        i = sys.argv.index("--role")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def load_config():
    """Program defaults → role preset → the user's own config.local.json."""
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg.update({"role": None, "density_example": "測試沒過就先修，不用問我", "switching_is_the_job": False})
    try:
        with open(CONFIG_LOCAL, encoding="utf-8") as f:
            local = json.load(f)
    except FileNotFoundError:
        local = {}
    except json.JSONDecodeError as exc:
        sys.exit(f"config.local.json 格式錯誤：{exc}")

    role = cli_role() or local.get("role")
    role = ROLE_ALIASES.get(role, role)
    if role is not None and role not in ROLE_PRESETS:
        sys.exit(f"role 應為 {'、'.join(ROLE_PRESETS)} 其中之一，收到：{role}")
    preset = ROLE_PRESETS.get(role or "通用", {})
    cfg["role"] = role
    if "density" in preset:
        cfg["risk_levels"]["density"] = preset["density"]
    if "high_risk_keywords" in preset:
        cfg["high_risk_keywords"] = {k: 3 for k in preset["high_risk_keywords"]}
    cfg["irreversible_patterns"] = cfg["irreversible_patterns"] + preset.get("irreversible_extra", [])
    for k in ("density_example", "switching_is_the_job"):
        if k in preset:
            cfg[k] = preset[k]

    for k, v in local.items():
        if k.startswith("_") or k == "role":
            continue
        # risk_levels merges per stage, so overriding one stage keeps the others.
        # high_risk_keywords / irreversible_patterns are replaced whole: a user's list is their list.
        if k == "risk_levels" and isinstance(v, dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def resolve_tz(value):
    if not value:
        return datetime.now().astimezone().tzinfo
    m = re.fullmatch(r"([+-])(\d{1,2}):(\d{2})", value.strip())
    if not m:
        sys.exit(f"timezone 設定格式應為 +08:00 這種形式，收到：{value}")
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


CFG = load_config()
TZ = resolve_tz(CFG["timezone"])
CLAUDE_PROJECTS = os.path.expanduser(CFG["claude_projects"])
CODEX_SESSIONS = os.path.expanduser(CFG["codex_sessions"])
HOME = os.path.expanduser("~").replace("\\", "/").rstrip("/").lower()

# Fixed on purpose (spec §4): literature thresholds, and the block split that
# every other number depends on. Making these configurable would make pages
# incomparable between people.
IDLE_MIN = 10                  # Albulescu et al. 2022: breaks under 10 min don't restore
SUSTAINED_CONTROL_HOURS = 6    # Blain et al. 2016: borrowed lab threshold
WINDOW_MIN = 15                # sliding window for switch storms / concurrency
SPIKE_MIN_REPLIES = 20         # below this a 90th percentile means nothing
TABLE_MIN_BLOCK_MIN = 10
TABLE_MIN_BLOCK_MSGS = 3
JUDGEMENT_MIN_LONG_REPLIES = 10  # fewer long outputs than this and a ratio means nothing

STAGES = [("density", "① 決策密度"), ("recovery", "② 恢復窗口"),
          ("fatigue", "③ 認知疲勞跡象"), ("judgement", "④ 判斷品質")]
LEVEL_ORDER = {"低": 0, "中": 1, "高": 2}

# Fixed text triggered by rules — never generated on the fly, so days stay
# comparable. Each one starts from a cognitive-science mechanism, and each was
# checked for (1) being doable in a one-person, turn-by-turn chat with an AI and
# (2) not contradicting another stage's advice. Ego depletion / "willpower runs
# out" is deliberately absent: it failed large preregistered replications.
SUGGESTIONS = [
    {"stage": "density", "when": {"高"},
     "text": "今天很多小事都跑來等你拍板。可以先幫常見情況定好規則，像「{example}」，AI 照著走，你的腦袋就能留給真正要想的事。",
     "why": "事先想好「如果…就…」，當下就不必再判斷一次", "cite": "執行意圖，Gollwitzer & Sheeran 2006 後設分析（信心高）"},
    {"stage": "density", "when": {"中", "高"},
     "text": "一開始把要求講清楚：範圍、限制、做到哪裡算完成。AI 中途來問你的次數會少很多。",
     "why": "目標越具體，表現越好", "cite": "目標設定理論，Locke & Latham 2002（信心中高）"},
    {"stage": "recovery", "when": {"中", "高"},
     "text": "已經連續好一陣子沒停了。下一件事開始前，起來走走、看看窗外，離開螢幕十分鐘再回來。",
     "why": "十分鐘以上的休息才有恢復效果；看遠處、看綠色有助於恢復專注力",
     "cite": "Albulescu et al. 2022（信心中高）；注意力恢復理論，Kaplan 1995（信心中）"},
    {"stage": "recovery", "when": {"高"},
     "text": "明天試著幫自己留一兩段不被打擾的時間，給需要專心想的事。",
     "why": "常被打斷的人會加快速度補回時間，代價是壓力和挫折感上升", "cite": "Mark, Gudith & Klocke 2008（信心中高）"},
    {"stage": "fatigue", "when": {"中", "高"},
     "text": "要切去別的對話之前，花半分鐘寫下「做到哪、下一步是什麼」。回來接得上，離開時心裡也比較放得下。",
     "why": "切換前寫下接續計畫，注意力比較不會卡在上一件事", "cite": "接續計畫，Leroy & Glomb 2018（信心中高）"},
    {"stage": "fatigue", "when": {"高"}, "skip_if_switching_is_the_job": True,
     "text": "同時開著的對話有點多，先收掉幾個，留兩個就好。",
     "why": "大腦同時只能抓住大約四件事，每個開著的對話都在佔位子",
     "cite": "工作記憶容量，Cowan 2001（信心中高）；「兩個」是經驗值"},
    {"stage": "judgement", "when": {"中", "高"},
     "text": "AI 寫了一大段的時候，給自己一點時間讀完再回。可以問自己：「要跟同事解釋的話，我講得出為什麼同意嗎？」",
     "why": "知道要為決定負責時，比較不會照單全收機器的建議", "cite": "問責效應，Skitka, Mosier & Burdick 2000（信心中）"},
    {"stage": "judgement", "when": {"topic_unverified"},
     "text": "碰到比較重大的決定，請 AI 反過來列出「這樣做可能哪裡不對」，看完再決定。",
     "why": "刻意想想反面，是少數被重複驗證有效的去偏誤方法", "cite": "考慮反面，Lord, Lepper & Preston 1984（信心中高）"},
]

EXPLORE_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "ToolSearch"}
PRODUCE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
# Only these toolDenialKinds are the person stopping the AI; permission-rule and
# automode-* are the harness enforcing policy, not a human decision.
STOPPED_AI_KINDS = {"user-rejected", "cancelled"}
REVIEW_SKILL_TOKENS = {"review", "audit", "security", "verify", "verification"}

# Synthetic content injected into the human's turn is wrapped in a closed tag
# (<system-reminder>, <command-message>, <scheduled-task>, <environment_context>…)
# and can share a text block with words the person really typed — strip the
# spans and keep the remainder rather than dropping the whole message.
TAG_SPAN = re.compile(r"<([A-Za-z][\w-]*)\b[^>]*>.*?</\1\s*>", re.S)


# ---------------------------------------------------------------- helpers

def parse_dt(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)
    except (ValueError, AttributeError):
        return None


def day_bounds(date_str):
    start = datetime.fromisoformat(date_str).replace(tzinfo=TZ)
    return start, start + timedelta(days=1)


def segment_of(dt):
    for seg in CFG["time_segments"]:
        a, b = seg["start"], seg["end"]
        if (a <= dt.hour < b) if a < b else (dt.hour >= a or dt.hour < b):
            return seg
    return None


def is_late(dt):
    seg = segment_of(dt)
    return bool(seg and seg.get("late_night"))


def text_blocks(content):
    if isinstance(content, str):
        return [content], False
    texts, media = [], False
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t in ("text", "input_text", "output_text"):
                texts.append(b.get("text") or "")
            elif t in ("image", "document", "input_image"):
                media = True
    return texts, media


def human_text(content):
    texts, media = text_blocks(content)
    return TAG_SPAN.sub("", "\n".join(texts)).strip(), media


def prose_len(content):
    return sum(len(t) for t in text_blocks(content)[0])


def topic_hits(text):
    low = text.lower()
    return [k for k in CFG["high_risk_keywords"] if k.lower() in low]


def project_label(cwd):
    """Display only — attention switching is measured between conversations."""
    if not cwd:
        return "unknown"
    p = cwd.replace("\\", "/").rstrip("/")
    if p.lower() == HOME:
        return "~（家目錄）"
    parts = p.split("/")
    base = parts[-1]
    if base.lower() in {d.lower() for d in CFG["generic_dirnames"]} and len(parts) >= 2:
        return f"{parts[-2]}/{base}"
    return base


def is_web_tool(name):
    n = name.lower()
    return name in ("WebFetch", "WebSearch") or n.endswith("__fetch") or "web_fetch" in n or "web_search" in n


def median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


# ---------------------------------------------------------------- Claude Code

def load_claude(date_str):
    """Events for the day grouped by conversation, fork/resume duplicates removed.

    A resume, fork or `change_directory` move copies the whole history into a NEW
    file with the same event uuids (observed 706/716 shared), which doubled every
    total and turned one continuous conversation into a fake ping-pong between two
    "projects". Files sharing any uuid are merged into one conversation and each
    uuid is counted once. Events without a uuid (queue-operation, frame-link,
    file-history-delta) were verified never to be copied between files.
    """
    start, end = day_bounds(date_str)
    loaded = []
    for path in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        try:
            if os.path.getmtime(path) < start.timestamp():  # append-only
                continue
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        evs = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            dt = parse_dt(d.get("timestamp", ""))
            if dt is not None and start <= dt < end:
                evs.append((dt, d))
        if evs:
            evs.sort(key=lambda e: e[0])
            loaded.append((evs[0][0], path, evs))

    loaded.sort(key=lambda x: (x[0], x[1]))
    owner, parent = {}, {}

    def root(f):
        while parent[f] != f:
            parent[f] = parent[parent[f]]
            f = parent[f]
        return f

    for _, path, evs in loaded:
        fid = os.path.splitext(os.path.basename(path))[0]
        parent[fid] = fid
        for _, d in evs:
            u = d.get("uuid")
            if not u:
                continue
            if u in owner:
                a, b = root(owner[u]), root(fid)
                if a != b:
                    parent[b] = a
            else:
                owner[u] = fid

    seen, duplicates, conversations = set(), 0, {}
    for _, path, evs in loaded:
        conv = root(os.path.splitext(os.path.basename(path))[0])
        for dt, d in evs:
            u = d.get("uuid")
            if u:
                if u in seen:
                    duplicates += 1
                    continue
                seen.add(u)
            conversations.setdefault(conv, []).append((dt, d))
    for evs in conversations.values():
        evs.sort(key=lambda e: e[0])
    merged = sum(1 for f in parent if root(f) != f)
    return conversations, duplicates, merged


def summarise_claude(sid, evs):
    tool_calls, skills = Counter(), Counter()
    cwd_human, cwd_all = Counter(), Counter()
    human, latencies, ai_times, actions = [], [], [], []
    s = Counter()
    pending, errored = {}, set()
    last_ai_dt, read_since_human = None, 0
    patterns = [p.lower() for p in CFG["irreversible_patterns"]]

    for dt, d in evs:
        typ = d.get("type")
        if d.get("cwd"):
            cwd_all[d["cwd"]] += 1
        if typ == "queue-operation":
            # enqueue/dequeue fire for every message sent (median gap 0.0s); only
            # remove is a signal — you queued something, then took it back.
            if d.get("operation") == "remove":
                s["changed_mind"] += 1
            continue
        if typ not in ("user", "assistant"):
            continue
        if d.get("toolDenialKind") in STOPPED_AI_KINDS:
            s["stopped_ai"] += 1
            actions.append({"dt": dt, "sid": sid, "kind": "stopped", "detail": d.get("toolDenialKind")})
        if d.get("attributionSkill"):
            skills[d["attributionSkill"]] += 1
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")

        if typ == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        err = b.get("is_error") is True
                        s["tool_errors"] += err
                        name = pending.pop(b.get("tool_use_id"), None)
                        if name:
                            (errored.add if err else errored.discard)(name)
            origin = d.get("origin")
            # origin.kind == "human" means "arrived on the human's turn" — scheduled
            # tasks are tagged human too — so the text must survive tag-stripping.
            if not (isinstance(origin, dict) and origin.get("kind") == "human"):
                continue
            text, media = human_text(content)
            if not (text or media):
                continue
            human.append({"dt": dt, "sid": sid, "len": len(text), "media": media,
                          # Everything the AI wrote since your previous message —
                          # the last assistant event alone is usually a bare tool_use.
                          "read_chars": read_since_human, "topics": topic_hits(text)})
            if d.get("cwd"):
                cwd_human[d["cwd"]] += 1
            if last_ai_dt is not None:
                gap = (dt - last_ai_dt).total_seconds()
                if 0 <= gap <= IDLE_MIN * 60:
                    latencies.append({"dt": dt, "gap": gap, "read_chars": read_since_human})
            last_ai_dt, read_since_human = None, 0
        else:
            ai_times.append(dt)
            last_ai_dt = dt
            n = prose_len(content)
            read_since_human += n
            s["assistant_chars"] += n
            usage = msg.get("usage") or {}
            s["output_tokens"] += usage.get("output_tokens") or 0
            s["cache_creation_tokens"] += usage.get("cache_creation_input_tokens") or 0
            s["thinking_tokens"] += (usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0
            if isinstance(content, list):
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    name = b.get("name", "unknown")
                    tool_calls[name] += 1
                    # Retry = same tool again after ITS OWN last call errored (a
                    # plain time window just measured call density).
                    if name in errored:
                        s["retries_after_error"] += 1
                    if b.get("id"):
                        pending[b["id"]] = name
                    if name == "AskUserQuestion":
                        actions.append({"dt": dt, "sid": sid, "kind": "ask", "detail": ""})
                    elif name in SHELL_TOOLS:
                        cmd = str((b.get("input") or {}).get("command", "")).lower()
                        hit = next((p for p in patterns if p in cmd), None)
                        if hit:
                            actions.append({"dt": dt, "sid": sid, "kind": "irreversible", "detail": hit})

    all_dts = [dt for dt, _ in evs]
    cwd = (cwd_human or cwd_all).most_common(1)[0][0] if (cwd_human or cwd_all) else None
    project = project_label(cwd)
    for h in human:
        h.update({"project": project, "tool": "Claude Code"})
    for a in actions:
        a["project"] = project
    return {
        "session_id": sid, "tool": "Claude Code", "project": project,
        "start": min(all_dts).isoformat(), "end": max(all_dts).isoformat(),
        "human_messages": len(human), "human_chars": sum(h["len"] for h in human),
        "assistant_chars": s["assistant_chars"],
        "tokens": s["output_tokens"] + s["cache_creation_tokens"],
        "thinking_tokens": s["thinking_tokens"],
        "tool_calls": sum(tool_calls.values()),
        "tool_breakdown": dict(tool_calls),  # full dict: low-frequency tools must not drop off
        # isSidechain is always False in the parent — it marks messages INSIDE a
        # subagent's own file. Spawning one leaves `tool_use: Agent` here.
        "agent_spawns": tool_calls.get("Agent", 0) + tool_calls.get("Task", 0),
        "ask_user_question": tool_calls.get("AskUserQuestion", 0),
        "web_checks": sum(n for t, n in tool_calls.items() if is_web_tool(t)),
        "explore_calls": sum(n for t, n in tool_calls.items() if t in EXPLORE_TOOLS or is_web_tool(t)),
        "produce_calls": sum(tool_calls.get(t, 0) for t in PRODUCE_TOOLS),
        "retries_after_error": s["retries_after_error"],
        "tool_errors": s["tool_errors"],
        "stopped_ai": s["stopped_ai"],
        "changed_mind": s["changed_mind"],
        "skills": dict(skills),
    }, human, latencies, ai_times, actions


# ---------------------------------------------------------------- Codex

def load_codex(date_str):
    """Human-driven Codex threads active on the day.

    Folder dates are when a thread STARTED, so every file is scanned and events
    are filtered by their own timestamp. Background threads (guardian
    auto-review, subagents) carry `parent_thread_id` and a `source` dict with a
    `subagent` key; their "user" turns are machine-written and are excluded.
    Codex logs no per-reply prose length, tokens or latency here — those metrics
    are Claude Code only and the page says so.
    """
    start, end = day_bounds(date_str)
    seen_ids, sessions, human_all, ai_all, background = set(), [], [], [], 0
    if not os.path.isdir(CODEX_SESSIONS):
        return sessions, human_all, ai_all, background
    for path in sorted(glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "*.jsonl"))):
        try:
            if os.path.getmtime(path) < start.timestamp():
                continue
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        meta, models_today, last_model, human, ai = {}, set(), None, [], []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = d.get("payload")
            if not isinstance(p, dict):
                continue
            typ = d.get("type")
            if typ == "session_meta":
                meta = p
                continue
            dt = parse_dt(d.get("timestamp", ""))
            today = dt is not None and start <= dt < end
            if typ == "event_msg" and p.get("type") == "thread_settings_applied":
                last_model = (p.get("thread_settings") or {}).get("model")
                if today:
                    models_today.add(last_model)
                continue
            if not today or typ != "response_item":
                continue
            pid = p.get("id")
            if pid:
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
            if p.get("type") == "message" and p.get("role") == "user":
                text, media = human_text(p.get("content"))
                if text or media:
                    human.append({"dt": dt, "len": len(text), "media": media,
                                  "read_chars": None, "topics": topic_hits(text)})
            elif p.get("type") != "message" or p.get("role") == "assistant":
                ai.append(dt)
        if not (human or ai):
            continue
        src = meta.get("source")
        if meta.get("parent_thread_id") or (isinstance(src, dict) and "subagent" in src):
            background += 1
            continue
        if not human:
            continue
        sid = "codex:" + os.path.splitext(os.path.basename(path))[0][-36:]
        project = project_label(meta.get("cwd"))
        for h in human:
            h.update({"sid": sid, "project": project, "tool": "Codex"})
        human_all += human
        ai_all += ai
        sessions.append({
            "session_id": sid, "tool": "Codex", "project": project,
            "models": sorted(m for m in (models_today or {last_model}) if m),
            "start": min(h["dt"] for h in human).isoformat(),
            "end": max(h["dt"] for h in human).isoformat(),
            "human_messages": len(human), "human_chars": sum(h["len"] for h in human),
        })
    return sessions, human_all, ai_all, background


# ---------------------------------------------------------------- day structure

def activity_blocks(human):
    """Blocks from when the person actually typed; > IDLE_MIN without input ends one."""
    if not human:
        return [], []
    blocks, breaks = [[human[0]["dt"], human[0]["dt"]]], []
    for prev, cur in zip(human, human[1:]):
        if (cur["dt"] - prev["dt"]).total_seconds() / 60 > IDLE_MIN:
            breaks.append({"start": prev["dt"], "end": cur["dt"]})
            blocks.append([cur["dt"], cur["dt"]])
        else:
            blocks[-1][1] = cur["dt"]
    return blocks, breaks


def annotate_breaks(breaks, ai_times):
    """No input isn't proof of absence — an agent may have been running while you
    watched. The longest stretch with neither human nor AI activity decides."""
    for br in breaks:
        marks = [br["start"]] + [t for t in ai_times if br["start"] < t < br["end"]] + [br["end"]]
        br["minutes"] = round((br["end"] - br["start"]).total_seconds() / 60, 1)
        br["longest_silent_min"] = round(max((b - a).total_seconds() / 60 for a, b in zip(marks, marks[1:])), 1)
        br["likely_away"] = br["longest_silent_min"] >= IDLE_MIN
    return breaks


def block_of(dt, blocks):
    for i, (a, b) in enumerate(blocks):
        if a <= dt <= b:
            return i
    return None


def switch_storms(human):
    """Densest windows of conversation-to-conversation switching, non-overlapping.
    A switch INTO a conversation already used earlier today is a "return" — the
    left-it-unfinished pattern attention residue is about."""
    seen_sids, switches = set(), []
    for p, c in zip(human, human[1:]):
        seen_sids.add(p["sid"])
        if c["sid"] != p["sid"]:
            c["is_return"] = c["sid"] in seen_sids
            switches.append((p, c))
    times = [c["dt"] for _, c in switches]
    cands, left = [], 0
    win = timedelta(minutes=WINDOW_MIN)
    for i, t in enumerate(times):
        while times[left] <= t - win:
            left += 1
        involved = []
        for p, c in switches[left:i + 1]:
            for x in (p, c):
                if x["project"] not in involved:
                    involved.append(x["project"])
        cands.append({"start": times[left], "end": t, "switches": i - left + 1,
                      "returns": sum(1 for _, c in switches[left:i + 1] if c.get("is_return")),
                      "projects": involved})
    cands.sort(key=lambda w: (-w["switches"], -w["returns"], w["start"]))
    picked = []
    for w in cands:
        if all(w["end"] < o["start"] or w["start"] > o["end"] for o in picked):
            picked.append(w)
    return switches, picked


def peak_concurrency(human):
    best, at, left = 0, None, 0
    win = timedelta(minutes=WINDOW_MIN)
    for i, h in enumerate(human):
        while human[left]["dt"] < h["dt"] - win:
            left += 1
        n = len({x["sid"] for x in human[left:i + 1]})
        if n > best:
            best, at = n, human[left]["dt"]
    return best, at


def hm(dt):
    return dt.strftime("%H:%M")


# ---------------------------------------------------------------- the day

def build(date_str):
    claude_raw, fork_dups, merged_files = load_claude(date_str)
    sessions, human, latencies, ai_times, actions = [], [], [], [], []
    for sid, evs in claude_raw.items():
        summ, h, lat, ai, act = summarise_claude(sid, evs)
        if summ["human_messages"] or summ["tool_calls"]:
            sessions.append(summ)
        human += h
        latencies += lat
        ai_times += ai
        actions += act
    codex_sessions, codex_human, codex_ai, codex_background = load_codex(date_str)
    human += codex_human
    ai_times += codex_ai
    human.sort(key=lambda h: h["dt"])
    ai_times.sort()
    actions.sort(key=lambda a: a["dt"])
    sessions.sort(key=lambda s: s["start"])
    by_sid = {s["session_id"]: s for s in sessions}
    all_sessions = sessions + codex_sessions

    def total(k):
        return sum(s.get(k, 0) for s in sessions)

    # --- stage 2 structure: activity blocks and real breaks
    blocks, breaks = activity_blocks(human)
    breaks = annotate_breaks(breaks, ai_times)
    spans = [round((b - a).total_seconds() / 60, 1) for a, b in blocks]
    active_min = round(sum(spans), 1)
    longest_block_min = max(spans, default=0)
    longest_idx = spans.index(longest_block_min) if spans else None

    for h in human:
        h["block"] = block_of(h["dt"], blocks)
        h["min_into_block"] = round((h["dt"] - blocks[h["block"]][0]).total_seconds() / 60, 1)

    # --- G7 high-importance messages: universal rules + optional personal topics
    replies = [h["read_chars"] for h in human if h.get("read_chars")]
    spike_on = len(replies) >= SPIKE_MIN_REPLIES
    spike_at = statistics.quantiles(replies, n=10, method="inclusive")[8] if spike_on else None
    for h in human:
        r = []
        if spike_on and (h.get("read_chars") or 0) > spike_at:
            r.append("資訊量爆量")
        if is_late(h["dt"]):
            r.append("深夜")
        if h["min_into_block"] >= CFG["fatigue_block_min"]:
            r.append("疲勞情境")
        if h["topics"]:
            r.append("高風險主題")
        h["reasons"] = r
    high = [h for h in human if h["reasons"]]
    reason_counts = Counter(r for h in high for r in h["reasons"])
    action_counts = Counter(a["kind"] for a in actions)

    # --- stage 1 time distribution
    seg_rows = []
    for seg in CFG["time_segments"]:
        hs = [h for h in human if segment_of(h["dt"]) is seg]
        seg_rows.append({"name": seg["name"], "range": f"{seg['start']:02d}–{seg['end']:02d}",
                         "messages": len(hs), "chars": sum(h["len"] for h in hs),
                         "late_night": bool(seg.get("late_night"))})
    hourly = Counter(h["dt"].hour for h in human)
    peak_hour = max(hourly, key=lambda k: (hourly[k], -k)) if hourly else None

    # --- stage 3 switching
    switches, storms = switch_storms(human)
    runs, run = [], 0
    for i, h in enumerate(human):
        run = run + 1 if i and h["sid"] == human[i - 1]["sid"] else 1
        if i + 1 == len(human) or human[i + 1]["sid"] != h["sid"]:
            runs.append(run)
    conc, conc_at = peak_concurrency(human)
    active_hours = active_min / 60 if active_min else None

    # --- stage 4 scrutiny (Claude Code only)
    fast = [x for x in latencies if x["gap"] < CFG["fast_accept_sec"]]
    long_replies = [x for x in latencies if x["read_chars"] >= CFG["long_output_chars"]]
    fast_long = [x for x in fast if x["read_chars"] >= CFG["long_output_chars"]]

    # verification around high-importance messages (Claude Code conversations only)
    verify_rows = []
    for sid in sorted({h["sid"] for h in high if h["tool"] == "Claude Code"}):
        s = by_sid.get(sid, {})
        sig = []
        if s.get("agent_spawns"):
            sig.append("派 subagent")
        if any(REVIEW_SKILL_TOKENS & set(re.split(r"[^a-z]+", k.lower())) for k in s.get("skills", {})):
            sig.append("review 類 skill")
        if s.get("web_checks"):
            sig.append("查外部來源")
        verify_rows.append({"project": s.get("project"), "high_messages": sum(1 for h in high if h["sid"] == sid),
                            "topic_messages": sum(1 for h in high if h["sid"] == sid and h["topics"]),
                            "signals": sig})
    topic_unverified = any(r["topic_messages"] and not r["signals"] for r in verify_rows)

    # --- G10: each block's conditions side by side (descriptive only)
    block_rows = []
    for i, (a, b) in enumerate(blocks):
        hs = [h for h in human if h["block"] == i]
        if spans[i] < TABLE_MIN_BLOCK_MIN or len(hs) < TABLE_MIN_BLOCK_MSGS:
            continue
        sw_in = sum(1 for _, c in switches if a <= c["dt"] <= b)
        lr = [x for x in long_replies if a <= x["dt"] <= b]
        fl = [x for x in fast_long if a <= x["dt"] <= b]
        block_rows.append({
            "start": hm(a), "end": hm(b), "minutes": spans[i],
            "segment": (segment_of(a) or {}).get("name"),
            "rest_before": breaks[i - 1]["minutes"] if i > 0 else None,
            "messages": len(hs),
            "switch_density": round(sw_in / (spans[i] / 60), 1),
            "median_len": median([h["len"] for h in hs if h["len"] > 0]),
            "fast_long": len(fl), "long_replies": len(lr),
            "high": sum(1 for h in hs if h["reasons"]),
            "tools": sorted({h["tool"] for h in hs}),
        })

    # --- risk level per stage: graded independently, never summed (spec §2)
    rl = CFG["risk_levels"]

    def grade(v, key):
        lo, hi = rl[key]
        return None if v is None else "低" if v < lo else "中" if v <= hi else "高"

    def floor_at(level, floor):
        return floor if level is None or LEVEL_ORDER[level] < LEVEL_ORDER[floor] else level

    late_n = sum(1 for h in human if is_late(h["dt"]))
    msgs_per_hour = round(len(human) / active_hours, 1) if active_hours else None
    sw_per_hour = round(len(switches) / active_hours, 1) if active_hours else None
    ratio = round(len(fast_long) / len(long_replies), 3) if long_replies else None
    enough_long = len(long_replies) >= JUDGEMENT_MIN_LONG_REPLIES

    levels = {
        "density": {"value": msgs_per_hour, "level": grade(msgs_per_hour, "density"), "notes": [],
                    "basis": "經驗值"},
        "recovery": {"value": longest_block_min if blocks else None,
                     "level": grade(longest_block_min, "recovery") if blocks else None, "notes": [],
                     "basis": "10 分鐘、6 小時有研究依據；分級是經驗值"},
        "fatigue": {"value": sw_per_hour, "level": grade(sw_per_hour, "fatigue"), "notes": [],
                    "basis": "經驗值"},
        "judgement": {"value": ratio, "level": grade(ratio, "judgement") if enough_long else None, "notes": [],
                      "basis": "經驗值"},
    }
    if blocks and longest_block_min >= SUSTAINED_CONTROL_HOURS * 60:
        levels["recovery"]["level"] = "高"
        levels["recovery"]["notes"].append(f"超過 {SUSTAINED_CONTROL_HOURS} 小時")
    if late_n and levels["fatigue"]["level"]:
        levels["fatigue"]["level"] = floor_at(levels["fatigue"]["level"], "中")
        levels["fatigue"]["notes"].append("有深夜使用")
    if not enough_long:
        levels["judgement"]["notes"].append("樣本不足" if long_replies or latencies else "無法判斷")
    elif topic_unverified:
        levels["judgement"]["level"] = floor_at(levels["judgement"]["level"], "中")
        levels["judgement"]["notes"].append("高風險主題沒有驗證")
    for key, lv in levels.items():
        lv["thresholds"] = rl[key]
        if lv["value"] is None and not lv["notes"]:
            lv["notes"].append("無法判斷")

    suggestions = []
    for key, _ in STAGES:
        lv = levels[key]["level"]
        for sug in SUGGESTIONS:
            if sug["stage"] != key:
                continue
            hit = (lv in sug["when"]) or ("topic_unverified" in sug["when"] and topic_unverified)
            # For roles where switching IS the job, "keep two conversations open"
            # can't be done — they only get the resume-plan advice.
            if sug.get("skip_if_switching_is_the_job") and CFG["switching_is_the_job"]:
                hit = False
            if hit:
                suggestions.append({"stage": sug["stage"], "why": sug["why"], "cite": sug["cite"],
                                    "text": sug["text"].format(example=CFG["density_example"]),
                                    "level": lv if lv in sug["when"] else "追加"})

    # --- right now (only while the day is still going)
    now = datetime.now(TZ)
    now_block = None
    if human and now.date().isoformat() == date_str:
        since_last = (now - human[-1]["dt"]).total_seconds() / 60
        active = since_last <= IDLE_MIN
        last_break = next((b for b in reversed(breaks) if b["likely_away"]), None)
        cur_min = round((human[-1]["dt"] - blocks[-1][0]).total_seconds() / 60, 1) if active else 0
        now_block = {
            "at": hm(now), "active_now": active, "minutes_since_last_message": round(since_last, 1),
            "current_block_min": cur_min,
            "over_fatigue": active and cur_min >= CFG["fatigue_block_min"],
            "switches_last_window": sum(1 for _, c in switches if c["dt"] > now - timedelta(minutes=WINDOW_MIN)),
            "last_real_break": {"start": hm(last_break["start"]), "end": hm(last_break["end"]),
                                "minutes": last_break["minutes"]} if last_break else None,
        }

    compressed = []
    for h in human:
        if compressed and compressed[-1]["sid"] == h["sid"]:
            compressed[-1]["n"] += 1
            compressed[-1]["end"] = hm(h["dt"])
        else:
            compressed.append({"sid": h["sid"], "project": h["project"], "tool": h["tool"], "n": 1,
                               "start": hm(h["dt"]), "end": hm(h["dt"])})

    return {
        "date": date_str,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "is_today": now.date().isoformat() == date_str,
        "config": {"role": CFG["role"], "personal_topics": len(CFG["high_risk_keywords"]),
                   "fatigue_block_min": CFG["fatigue_block_min"],
                   "long_output_chars": CFG["long_output_chars"], "fast_accept_sec": CFG["fast_accept_sec"],
                   "time_segments": CFG["time_segments"]},
        "sources": {"claude_conversations": len(sessions), "codex_conversations": len(codex_sessions),
                    "claude_fork_duplicates_removed": fork_dups, "claude_files_merged": merged_files,
                    "codex_background_threads": codex_background},
        "now": now_block,
        "levels": levels,
        "suggestions": suggestions,
        "stage1_density": {
            "conversations": len(all_sessions),
            "human_messages": len(human), "human_chars": sum(h["len"] for h in human),
            "assistant_chars": total("assistant_chars"), "tokens": total("tokens"),
            "thinking_tokens": total("thinking_tokens"),
            "ai_asked_you": total("ask_user_question"), "changed_mind": total("changed_mind"),
            "stopped_ai": total("stopped_ai"),
            "high_messages": len(high), "reason_counts": dict(reason_counts),
            "spike_rule_on": spike_on, "spike_threshold_chars": round(spike_at) if spike_at else None,
            "actions": dict(action_counts),
            "irreversible_detail": dict(Counter(a["detail"] for a in actions if a["kind"] == "irreversible")),
            "segments": seg_rows, "hourly": {f"{k:02d}": hourly[k] for k in sorted(hourly)},
            "peak_hour": f"{peak_hour:02d}" if peak_hour is not None else None,
            "peak_hour_messages": hourly[peak_hour] if peak_hour is not None else 0,
        },
        "stage2_recovery": {
            "active_min": active_min, "longest_block_min": longest_block_min,
            "exceeds_sustained_threshold": longest_block_min >= SUSTAINED_CONTROL_HOURS * 60,
            "real_breaks": sum(1 for b in breaks if b["likely_away"]),
            "longest_break_min": max((b["minutes"] for b in breaks if b["likely_away"]), default=0),
            "blocks": [{"start": hm(a), "end": hm(b), "minutes": m} for (a, b), m in zip(blocks, spans)],
            "breaks": [{"start": hm(b["start"]), "end": hm(b["end"]), "minutes": b["minutes"],
                        "likely_away": b["likely_away"]} for b in breaks],
        },
        "stage3_fatigue": {
            "switches": len(switches), "returns": sum(1 for _, c in switches if c.get("is_return")),
            "per_active_hour": round(len(switches) / active_hours, 1) if active_hours else None,
            "longest_focus_run": max(runs, default=0),
            "peak_concurrent": conc, "peak_concurrent_at": hm(conc_at) if conc_at else None,
            "sharpest_window": ({"start": hm(storms[0]["start"]), "end": hm(storms[0]["end"]),
                                 "switches": storms[0]["switches"], "returns": storms[0]["returns"]}
                                if storms else None),
            "median_msg_len": median([h["len"] for h in human if h["len"] > 0]),
            "length_by_block": [{"start": hm(a), "messages": sum(1 for h in human if h["block"] == i),
                                 "median_len": median([h["len"] for h in human if h["block"] == i and h["len"] > 0])}
                                for i, (a, b) in enumerate(blocks)],
            "late_night_messages": sum(1 for h in human if is_late(h["dt"])),
        },
        "stage4_judgement": {
            "replies": len(latencies), "median_reply_sec": median(sorted(x["gap"] for x in latencies)),
            "long_replies": len(long_replies), "fast_long": len(fast_long),
            "fast_long_ratio": round(len(fast_long) / len(long_replies), 3) if long_replies else None,
            "verification": verify_rows,
            "retries_after_error": total("retries_after_error"), "tool_errors": total("tool_errors"),
            "explore_calls": total("explore_calls"), "produce_calls": total("produce_calls"),
            "agent_spawns": total("agent_spawns"),
        },
        "block_table": block_rows,
        "switch_sequence": compressed,
        "sessions": all_sessions,
    }


# ---------------------------------------------------------------- dashboard

ACTION_LABEL = {"ask": "AI 停下來要你決定", "stopped": "你中止 AI 的動作", "irreversible": "不可逆指令"}


def _f(v, digits=1):
    if v is None:
        return "—"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:.{digits}f}"
    return f"{int(v):,}" if isinstance(v, (int, float)) else str(v)


def _min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def summary_line(out):
    d1, d2 = out["stage1_density"], out["stage2_recovery"]
    if not d1["human_messages"]:
        return f"{out['date']} 沒有使用 AI 的紀錄。"
    peak = (f"對話最密集在 {d1['peak_hour']}:00–{int(d1['peak_hour']) + 1:02d}:00（{d1['peak_hour_messages']} 則）"
            if d1["peak_hour"] else "")
    names = {"density": "決策密度", "recovery": "恢復窗口", "fatigue": "疲勞跡象", "judgement": "判斷品質"}
    graded = "、".join(f"{names[k]}{v['level'] or '無法判斷'}" for k, v in out["levels"].items())
    day = "今天" if out["is_today"] else "這天"
    return (f"{day} {d1['conversations']} 段對話、實際對話 {_f(d2['active_min'])} 分鐘；{peak}；"
            f"最長一段沒休息 {_f(d2['longest_block_min'])} 分鐘。四個階段：{graded}。")


def render_html(out):
    e = html.escape
    d1, d2, d3, d4 = out["stage1_density"], out["stage2_recovery"], out["stage3_fatigue"], out["stage4_judgement"]
    cfg = out["config"]
    date = datetime.fromisoformat(out["date"])
    wd = "一二三四五六日"[date.weekday()]
    both, cc = '<span class="cov">兩者</span>', '<span class="cov cc">僅 Claude Code</span>'

    def stat(label, value, unit="", cov=""):
        return (f'<div class="stat"><div class="s-l">{e(label)} {cov}</div>'
                f'<div class="s-v">{value}<small>{e(unit)}</small></div></div>')

    # ---- graded chain: the mechanism as a flow, each stage coloured by its own level
    ratio = d4["fast_long_ratio"]
    lvls = out["levels"]
    cls_of = {"低": "lo", "中": "mi", "高": "hi"}
    units = {"density": "則／活躍小時", "recovery": "分鐘沒有 10 分鐘休息",
             "fatigue": "次切換／活躍小時", "judgement": f"長內容秒回（{d4['fast_long']}／{d4['long_replies']}）"}
    nodes = []
    for n_i, (key, title) in enumerate(STAGES, 1):
        lv = lvls[key]
        lo, hi = lv["thresholds"]
        pct = key == "judgement"
        v = lv["value"]
        shown = (f"{v * 100:.0f}%" if pct else _f(v)) if v is not None else "—"
        tag = lv["level"] or "、".join(lv["notes"]) or "無法判斷"
        scale = ""
        if v is not None:
            vmax = hi * 1.6
            pos = lambda x: max(0.0, min(100.0, x / vmax * 100))  # noqa: E731
            scale = (f'<div class="rs"><span class="rs-lo" style="width:{pos(lo):.1f}%"></span>'
                     f'<span class="rs-mi" style="width:{pos(hi) - pos(lo):.1f}%"></span><span class="rs-hi"></span>'
                     f'<i class="rs-pin" style="left:{pos(v):.1f}%"></i></div>'
                     f'<div class="rs-cap"><span style="left:{pos(lo):.1f}%">{f"{lo * 100:.0f}%" if pct else _f(lo)}</span>'
                     f'<span style="left:{pos(hi):.1f}%">{f"{hi * 100:.0f}%" if pct else _f(hi)}</span></div>'
                     f'<div class="rs-basis">門檻：{"研究＋經驗值" if key == "recovery" else "經驗值"}</div>')
        extra = f'<div class="rn">{e("、".join(lv["notes"]))}</div>' if lv["level"] and lv["notes"] else ""
        nodes.append(
            f'<a class="node {cls_of.get(lv["level"], "na")}" href="#s{n_i}"><span class="n-t">{e(title)}</span>'
            f'<span class="n-row"><span class="n-v">{shown}</span><span class="n-tag">{e(tag)}</span></span>'
            f'<span class="n-u">{e(units[key])}</span>{scale}{extra}</a>')
    chain_html = ('<nav class="flow" aria-label="四階段風險分級">'
                  + '<span class="arrow" aria-hidden="true">→</span>'.join(nodes) + "</nav>"
                  + '<div class="legend"><span><i class="sw lo"></i>低</span><span><i class="sw mi"></i>中</span>'
                    '<span><i class="sw hi"></i>高</span><span>直線＝今天的數值落點；四個階段各自分級，不加總</span></div>')

    # ---- rule-triggered suggestions, in a caring voice (fixed text, never generated)
    sugs = out["suggestions"]
    if sugs:
        items = "".join(
            f'<li><span class="s-tag {cls_of.get(s["level"], "mi")}">{e(dict(STAGES)[s["stage"]][:1])} {e(s["level"])}</span>'
            f'<div><div class="s-text">{e(s["text"])}</div>'
            f'<div class="s-why">{e(s["why"])} · {e(s["cite"])}</div></div></li>' for s in sugs)
        sug_html = (f'<section class="care"><h2 class="care-h">今天想提醒你的幾件事</h2>'
                    f'<p class="care-sub">辛苦了。下面是從今天的節奏裡看到、可以試試看的小調整。</p>'
                    f'<ul class="sugs">{items}</ul></section>')
    else:
        sug_html = '<section class="care"><h2 class="care-h">今天的節奏還不錯，繼續保持。</h2></section>'

    # ---- now
    now_html = ""
    n = out.get("now")
    if n:
        if n["active_now"]:
            lb = n["last_real_break"]
            parts = [f"這一段已連續 <b>{_f(n['current_block_min'])} 分鐘</b>",
                     f"最近 {WINDOW_MIN} 分鐘切換 <b>{n['switches_last_window']}</b> 次",
                     f"上次 {IDLE_MIN} 分鐘以上的休息：{lb['start']}–{lb['end']}" if lb else "今天還沒有 10 分鐘以上的休息"]
            txt = "；".join(parts)
            if n["over_fatigue"]:
                txt += f'<div class="now-fact">距離上次 {IDLE_MIN} 分鐘以上的休息已經 {_f(n["current_block_min"])} 分鐘。</div>'
        else:
            txt = f"已經 {_f(n['minutes_since_last_message'])} 分鐘沒有對話"
        now_html = f'<section class="now"><span class="now-at">此刻 {e(n["at"])}</span><div>{txt}</div></section>'

    # ---- stage 1
    high_pct = f"{d1['high_messages'] / d1['human_messages'] * 100:.0f}%" if d1["human_messages"] else "—"
    reason_chips = []
    for r, note in (("資訊量爆量", "" if d1["spike_rule_on"] else "（當天回覆少於 20 次，此規則未啟用）"),
                    ("深夜", ""), ("疲勞情境", f"（所在活動段已連續 ≥ {cfg['fatigue_block_min']} 分鐘）"),
                    ("高風險主題", "" if cfg["personal_topics"] else "（未設定關鍵字）")):
        reason_chips.append(f'<li><span>{e(r)}{e(note)}</span><b>{d1["reason_counts"].get(r, 0)}</b></li>')
    act = d1["actions"]
    irr = d1["irreversible_detail"]
    irr_txt = f"（{'、'.join(f'{k} {v}' for k, v in sorted(irr.items(), key=lambda kv: -kv[1]))}）" if irr else ""
    action_rows = "".join(
        f'<li><span>{e(ACTION_LABEL[k])}{e(irr_txt) if k == "irreversible" else ""}</span><b>{act.get(k, 0)}</b></li>'
        for k in ACTION_LABEL)
    hours = [int(h) for h in d1["hourly"]]
    hmax = max(d1["hourly"].values(), default=1)
    hbars = ""
    if hours:
        for h in range(min(hours), max(hours) + 1):
            c = d1["hourly"].get(f"{h:02d}", 0)
            peak = " peak" if f"{h:02d}" == d1["peak_hour"] else ""
            hbars += (f'<div class="hcol"><span class="hn">{c or ""}</span>'
                      f'<div class="hbar{peak}" style="height:{c / hmax * 100:.0f}%"></div><span class="hx">{h:02d}</span></div>')
    seg_rows = "".join(
        f'<tr><td>{e(s["name"])}{"（深夜）" if s["late_night"] and s["name"] != "深夜" else ""}</td>'
        f'<td class="num">{s["range"]}</td><td class="num">{s["messages"]}</td><td class="num">{_f(s["chars"])}</td></tr>'
        for s in d1["segments"])
    spike_note = (f"資訊量爆量＝你發這則之前 AI 累積寫的字超過當天第 90 百分位（今天是 {_f(d1['spike_threshold_chars'])} 字）。"
                  if d1["spike_rule_on"] else "")

    stage1 = f"""
<section id="s1" class="stage"><div class="st-h"><span class="st-n">1</span><div><h2>決策密度</h2>
<p class="st-q">AI 讓決策密度暴增——今天有多少事經過你的判斷？</p></div></div>
<div class="grid6">{stat("對話段數", d1['conversations'], "段", both)}{stat("你發的訊息", _f(d1['human_messages']), "則", both)}
{stat("你打的字", _f(d1['human_chars']), "字", both)}{stat("AI 回的字", _f(d1['assistant_chars']), "字", cc)}
{stat("資訊量 token", _f(d1['tokens']), "", cc)}{stat("思考量 thinking token", _f(d1['thinking_tokens']), "", cc)}</div>
<div class="two">
<div class="card"><h3>決策事件 {cc}</h3><ul class="kv">
<li><span>AI 停下來要你決定</span><b>{d1['ai_asked_you']}</b></li>
<li><span>你改變主意（排了指令又取消）</span><b>{d1['changed_mind']}</b></li>
<li><span>你中止 AI 的動作</span><b>{d1['stopped_ai']}</b></li></ul></div>
<div class="card"><h3>一般 vs 高度重要</h3>
<div class="split"><div><div class="big high">{d1['high_messages']}</div><div class="s-l">高度重要訊息（佔 {high_pct}）{both}</div></div>
<div><div class="big">{d1['human_messages'] - d1['high_messages']}</div><div class="s-l">一般訊息</div></div></div>
<ul class="kv small">{"".join(reason_chips)}</ul>
<h4>高度重要動作 {cc}</h4><ul class="kv small">{action_rows}</ul>
<p class="note">{e(spike_note)}一則訊息可能同時符合多條規則。</p></div></div>
<div class="two">
<div class="card"><h3>早中晚 {both}</h3><table><tr><th>時段</th><th class="num">時間</th><th class="num">訊息</th><th class="num">你打的字</th></tr>{seg_rows}</table></div>
<div class="card"><h3>每小時訊息量 {both}</h3><div class="hchart">{hbars}</div>
<p class="note">深色＝最密集的一小時。</p></div></div>
</section>"""

    # ---- stage 2 rhythm strip
    strip = ""
    if d2["blocks"]:
        t0 = _min(d2["blocks"][0]["start"]) // 60 * 60
        t1 = (_min(d2["blocks"][-1]["end"]) // 60 + 1) * 60
        span = max(t1 - t0, 60)

        def x(t):
            return (_min(t) - t0) / span * 100

        segs = []
        for b in d2["breaks"]:
            cls = "brk" if b["likely_away"] else "brk soft"
            lbl = f'<span class="brk-lbl">{_f(b["minutes"], 0)} 分</span>' if b["minutes"] >= 30 else ""
            segs.append(f'<div class="{cls}" style="left:{x(b["start"]):.2f}%;width:{x(b["end"]) - x(b["start"]):.2f}%" '
                        f'title="{b["start"]}→{b["end"]}，{_f(b["minutes"])} 分鐘沒有輸入">{lbl}</div>')
        for b in d2["blocks"]:
            segs.append(f'<div class="blk" style="left:{x(b["start"]):.2f}%;width:{max(x(b["end"]) - x(b["start"]), 0.4):.2f}%" '
                        f'title="{b["start"]}–{b["end"]}，{_f(b["minutes"])} 分鐘"></div>')
        hrs = list(range(t0, t1 + 1, 60))
        ticks = "".join(f'<span style="left:{(h - t0) / span * 100:.2f}%;transform:translateX('
                        f'{"0" if j == 0 else "-100%" if j == len(hrs) - 1 else "-50%"})">{h // 60:02d}:00</span>'
                        for j, h in enumerate(hrs))
        strip = (f'<div class="strip-wrap"><div class="strip-inner">'
                 f'<div class="strip">{"".join(segs)}</div><div class="ticks">{ticks}</div></div></div>'
                 f'<div class="legend"><span><i class="sw a"></i>實際在對話</span><span><i class="sw b"></i>真的離開</span>'
                 f'<span><i class="sw c"></i>沒輸入但 AI 還在跑</span></div>')

    stage2 = f"""
<section id="s2" class="stage"><div class="st-h"><span class="st-n">2</span><div><h2>恢復窗口</h2>
<p class="st-q">恢復窗口消失——今天有沒有真的停下來？</p></div></div>
<div class="grid4">{stat("實際對話時間", _f(d2['active_min']), "分", both)}{stat("最長一段沒休息", _f(d2['longest_block_min']), "分", both)}
{stat("真正離開", d2['real_breaks'], "次", both)}{stat("最長一次休息", _f(d2['longest_break_min']), "分", both)}</div>
<div class="card">{strip}</div>
<p class="ref">「真正離開」＝連續 {IDLE_MIN} 分鐘以上沒有你的輸入、AI 也沒在跑。{IDLE_MIN} 分鐘對齊 Albulescu et al. (2022)：不到 10 分鐘的休息不足以讓高認知需求的工作恢復（信心中高）。持續高強度認知控制約 {SUSTAINED_CONTROL_HOURS} 小時後前額葉出現代價（Blain et al. 2016，借用的實驗室門檻，信心中）——今天{"已" if d2['exceeds_sustained_threshold'] else "未"}超過。</p>
</section>"""

    # ---- stage 3
    lbb = [b for b in d3["length_by_block"] if b["messages"]]
    lmax = max([b["median_len"] or 0 for b in lbb] or [1]) or 1
    lbars = "".join(f'<div class="lcol"><span class="ln">{_f(b["median_len"], 0)}</span>'
                    f'<div class="lbar" style="height:{(b["median_len"] or 0) / lmax * 100:.0f}%"></div>'
                    f'<span class="lx">{b["start"]}</span><span class="lx">{b["messages"]} 則</span></div>' for b in lbb)
    sw = d3["sharpest_window"]
    stage3 = f"""
<section id="s3" class="stage"><div class="st-h"><span class="st-n">3</span><div><h2>認知疲勞的跡象</h2>
<p class="st-q">注意力被切碎、指令變短、深夜還在用？</p></div></div>
<div class="grid4">{stat("切換", d3['switches'], "次", both)}{stat("其中跳回先前丟下的對話", d3['returns'], "次", both)}
{stat("切換密度", _f(d3['per_active_hour']), "次／活躍小時", both)}{stat("同時開著", d3['peak_concurrent'], f"段 @ {d3['peak_concurrent_at'] or '—'}", both)}</div>
<div class="two">
<div class="card"><h3>最密集的 {WINDOW_MIN} 分鐘 {both}</h3>
<p class="lead-s">{(f"{sw['start']}–{sw['end']}：{sw['switches']} 次切換，其中 {sw['returns']} 次跳回") if sw else "今天沒有切換密集的時窗"}</p>
<ul class="kv small"><li><span>最長不切換連續段</span><b>{d3['longest_focus_run']} 則</b></li>
<li><span>深夜訊息</span><b>{d3['late_night_messages']}</b></li></ul></div>
<div class="card"><h3>指令長度 · 每段活動的中位數字數 {both}</h3><div class="lchart">{lbars}</div>
<p class="note">過載時指令傾向變短、變含糊。全天中位數 {_f(d3['median_msg_len'], 0)} 字。</p></div></div>
<p class="ref">「跳回」＝切到今天稍早用過、中途離開的對話，最接近注意力殘留研究說的未閉合切換（Leroy 2009；Mark et al. 2008，信心中高）。切換以對話為單位，同一個視窗裡換資料夾不算。</p>
</section>"""

    # ---- stage 4
    vrows = "".join(f'<tr><td>{e(v["project"] or "")}</td><td class="num">{v["high_messages"]}</td>'
                    f'<td>{e("、".join(v["signals"]) or "無")}</td></tr>' for v in d4["verification"])
    ep = f"{d4['explore_calls'] / d4['produce_calls']:.2f}" if d4["produce_calls"] else "—"
    stage4 = f"""
<section id="s4" class="stage"><div class="st-h"><span class="st-n">4</span><div><h2>判斷品質</h2>
<p class="st-q">判斷品質下滑——AI 講完你有沒有真的看？</p></div></div>
<div class="grid4">{stat("長內容後秒回", _f(ratio * 100 if ratio is not None else None, 0), "%", cc)}{stat("長內容回覆次數", d4['long_replies'], "次", cc)}
{stat("回覆延遲中位數", _f(d4['median_reply_sec']), "秒", cc)}{stat("錯誤後重試", d4['retries_after_error'], f"／{d4['tool_errors']} 次錯誤", cc)}</div>
<div class="two">
<div class="card"><h3>高度重要訊息所在對話的驗證行為 {cc}</h3>
{("<table><tr><th>專案</th><th class='num'>高度重要訊息</th><th>驗證訊號</th></tr>" + vrows + "</table>") if vrows else '<p class="note">沒有 Claude Code 的高度重要訊息。</p>'}</div>
<div class="card"><h3>工作訊號（信心低中）{cc}</h3><ul class="kv small">
<li><span>探索／產出比（讀、查 vs 寫、改）</span><b>{ep}</b></li><li><span>派 subagent</span><b>{d4['agent_spawns']}</b></li></ul>
<p class="note">多讀少寫可能是謹慎，也可能只是任務本身要查很多。</p></div></div>
<p class="ref">「長內容後秒回」＝AI 自你上次發言後寫了 {cfg['long_output_chars']} 字以上，你 {cfg['fast_accept_sec']} 秒內就回覆（自動化自滿，Parasuraman &amp; Manzey 2010，信心中）。這一階段只看做判斷時的條件，不判斷決定對不對——對話紀錄沒有結果資料。</p>
</section>"""

    # ---- G10 block table
    trs = "".join(
        f'<tr><td>{r["start"]}–{r["end"]}</td><td>{e(r["segment"] or "")}</td><td class="num">{_f(r["minutes"])}</td>'
        f'<td class="num">{_f(r["rest_before"])}</td><td class="num">{r["messages"]}</td>'
        f'<td class="num">{_f(r["switch_density"])}</td><td class="num">{_f(r["median_len"], 0)}</td>'
        f'<td class="num">{(str(r["fast_long"]) + " / " + str(r["long_replies"])) if r["long_replies"] else "—"}</td>'
        f'<td class="num">{r["high"]}</td><td>{e("、".join(r["tools"]))}</td></tr>' for r in out["block_table"])
    table = f"""
<section class="stage"><h2 class="plain">每段活動的條件並排</h2>
<div class="card tbl"><table><tr><th>時間</th><th>時段</th><th class="num">長度（分）</th><th class="num">之前休息（分）</th>
<th class="num">訊息</th><th class="num">切換密度</th><th class="num">指令長度</th><th class="num">長內容秒回</th><th class="num">高度重要</th><th>工具</th></tr>{trs}</table></div>
<p class="note">只列長度 ≥ {TABLE_MIN_BLOCK_MIN} 分鐘且 ≥ {TABLE_MIN_BLOCK_MSGS} 則訊息的活動段。「長內容秒回」只有 Claude Code 有資料，沒有就顯示「—」。<b>單日只有幾段、時段又跟做什麼工作疊在一起，這張表只把條件並排給你看，無法回答「越晚是否越差」。</b></p>
</section>"""

    # ---- details
    src = out["sources"]
    srows = "".join(f'<tr><td>{s["start"][11:16]}–{s["end"][11:16]}</td><td>{e(s["tool"])}</td><td>{e(s["project"])}</td>'
                    f'<td class="num">{s["human_messages"]}</td><td class="num">{_f(s["human_chars"])}</td>'
                    f'<td class="num">{_f(s.get("assistant_chars"))}</td><td class="num">{_f(s.get("tool_calls"))}</td></tr>'
                    for s in out["sessions"])
    details = f"""
<details><summary>對話明細（{len(out['sessions'])} 段）</summary><div class="card tbl"><table>
<tr><th>時間</th><th>工具</th><th>專案</th><th class="num">你發話</th><th class="num">你打的字</th><th class="num">AI 回的字</th><th class="num">工具呼叫</th></tr>{srows}</table></div></details>
<details><summary>方法與限制</summary><div class="card method">
<p>資料只來自這台電腦上的 Claude Code 與 Codex 對話紀錄。同一段對話被續寫、分叉或換資料夾時會被複製成新檔案，已合併去重（合併 {src['claude_files_merged']} 個檔案、去掉 {_f(src['claude_fork_duplicates_removed'])} 筆重複事件）。Codex 的背景自動審查與子代理（{src['codex_background_threads']} 條）不算你的使用。</p>
<p>只算你真正打的字：系統自動插入的內容（例如 system-reminder、排程任務、slash command 輸出）會先剝除。</p>
<p>Codex 的紀錄沒有 AI 回覆字數、token 與回覆延遲，標「僅 Claude Code」的指標只反映 Claude Code 的使用。</p>
<p>「認知負荷」刻意不做成單一分數——恢復窗口、疲勞跡象、判斷品質是不同的東西，加起來會製造假精確。</p>
<p>門檻：{IDLE_MIN} 分鐘有效休息（Albulescu et al. 2022）、{SUSTAINED_CONTROL_HOURS} 小時持續控制（Blain et al. 2016，借用）有文獻依據；疲勞情境 {cfg['fatigue_block_min']} 分鐘、長內容 {cfg['long_output_chars']} 字、秒回 {cfg['fast_accept_sec']} 秒是工程慣例，可在 config.local.json 調整。</p>
<p>刻意不做：跨日比較、單一總分、判斷決策對不對、情緒推論、AI 生成的建議句、主動推播。</p>
</div></details>"""

    css = """
:root{--bg:#F4F6F3;--surface:#FFFFFF;--text:#1B211D;--muted:#67716B;--border:#DDE3DC;--track:#ECEFEA;
--accent:#2F5D62;--accent-soft:#DCEAE8;--high:#B5541F;--high-soft:#F5E6DA;--cc:#3A6EA5;--lo:#3F7D4E;--lo-soft:#E0EDE1;--mi:#8A5A00;--mi-soft:#F6E7C4;--hi:#B5541F;--hi-soft:#F5E1D3;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#12161A;--surface:#1A1F23;--text:#ECEFEC;--muted:#96A39B;--border:#2A3238;--track:#20262A;
--accent:#6FB8B4;--accent-soft:#1F3333;--high:#E2915B;--high-soft:#3A2A1E;--cc:#7FA8D9;--lo:#7FBE8C;--lo-soft:#1E3324;--mi:#E0B45C;--mi-soft:#3A301A;--hi:#E2915B;--hi-soft:#3E2A1D;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#12161A;--surface:#1A1F23;--text:#ECEFEC;--muted:#96A39B;--border:#2A3238;--track:#20262A;
--accent:#6FB8B4;--accent-soft:#1F3333;--high:#E2915B;--high-soft:#3A2A1E;--cc:#7FA8D9;--lo:#7FBE8C;--lo-soft:#1E3324;--mi:#E0B45C;--mi-soft:#3A301A;--hi:#E2915B;--hi-soft:#3E2A1D;color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Noto Sans TC',-apple-system,'Segoe UI',sans-serif;padding-inline:16px;padding-block:32px;display:flex;justify-content:center;line-height:1.55}
.page{width:100%;max-width:1000px;display:flex;flex-direction:column;gap:28px}
.eyebrow{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
h1{font-family:'Fraunces',Georgia,serif;font-weight:600;font-size:clamp(26px,4vw,36px);margin:4px 0 0;text-wrap:balance}
.lead{font-size:16.5px;line-height:1.75;margin:0;max-width:72ch}
.flow{display:flex;align-items:stretch;gap:6px}
.node{flex:1 1 0;min-width:0;display:flex;flex-direction:column;gap:3px;text-decoration:none;color:inherit;background:var(--surface);border:1.5px solid var(--border);border-radius:12px;padding:12px 14px 10px}
.node:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.node.lo{border-color:var(--lo)}.node.mi{border-color:var(--mi)}.node.hi{border-color:var(--hi)}
.n-t{font-size:12px;font-weight:700;color:var(--muted)}
.n-row{display:flex;justify-content:space-between;align-items:baseline;gap:6px}
.n-v{font-family:'Fraunces',Georgia,serif;font-size:26px;font-weight:600;line-height:1.1;font-variant-numeric:tabular-nums}
.n-tag{font-size:12px;font-weight:700;padding:2px 10px;border-radius:999px;background:var(--track);color:var(--muted);white-space:nowrap}
.lo .n-tag{background:var(--lo-soft);color:var(--lo)}.mi .n-tag{background:var(--mi-soft);color:var(--mi)}.hi .n-tag{background:var(--hi-soft);color:var(--hi)}
.n-u{font-size:11.5px;color:var(--muted)}
.rs{position:relative;display:flex;height:6px;border-radius:3px;margin-top:8px}
.rs-lo{background:var(--lo-soft);border-radius:3px 0 0 3px}.rs-mi{background:var(--mi-soft)}.rs-hi{flex:1;background:var(--hi-soft);border-radius:0 3px 3px 0}
.rs-pin{position:absolute;top:-4px;width:3px;height:14px;background:var(--text);border-radius:2px;transform:translateX(-1px)}
.rs-cap{position:relative;height:15px;font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:var(--muted);margin-top:3px}
.rs-cap span{position:absolute;transform:translateX(-50%)}
.rs-basis{font-size:10.5px;color:var(--muted)}
.rn{font-size:11px;color:var(--muted)}
.arrow{align-self:center;color:var(--muted);font-size:18px}
@media (max-width:760px){.flow{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.arrow{display:none}}
.sw.lo{background:var(--lo-soft);border:1px solid var(--lo)}.sw.mi{background:var(--mi-soft);border:1px solid var(--mi)}.sw.hi{background:var(--hi-soft);border:1px solid var(--hi)}
.care{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px 20px}
.care-h{font-size:17px;margin:0}.care-sub{margin:4px 0 8px;font-size:13.5px;color:var(--muted)}
.sugs{list-style:none;margin:0;padding:0}
.sugs li{display:flex;gap:12px;align-items:flex-start;padding:12px 0;border-top:1px solid var(--border)}
.s-tag{font-size:12px;font-weight:700;padding:2px 8px;border-radius:6px;white-space:nowrap;margin-top:3px}
.s-tag.lo{background:var(--lo-soft);color:var(--lo)}.s-tag.mi{background:var(--mi-soft);color:var(--mi)}.s-tag.hi{background:var(--hi-soft);color:var(--hi)}
.s-text{font-size:15px;line-height:1.75}.s-why{font-size:12px;color:var(--muted);margin-top:2px}
.now{background:var(--accent-soft);border-radius:10px;padding:12px 16px;font-size:14.5px;display:flex;gap:12px;flex-wrap:wrap;align-items:baseline}
.now-at{font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--accent);font-weight:500}
.now-fact{margin-top:4px;font-weight:500;color:var(--high)}
.stage{display:flex;flex-direction:column;gap:12px;scroll-margin-top:16px}
.st-h{display:flex;gap:12px;align-items:flex-start;border-top:2px solid var(--text);padding-top:12px}
.st-n{font-family:'Fraunces',Georgia,serif;font-size:28px;font-weight:600;line-height:1;color:var(--accent)}
h2{font-size:19px;margin:0}.st-q{margin:2px 0 0;color:var(--muted);font-size:13.5px}
h2.plain{font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;border-top:1px solid var(--border);padding-top:14px}
h3{font-size:13.5px;margin:0 0 10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
h4{font-size:12.5px;margin:12px 0 6px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px;min-width:0}
.grid6{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.grid4{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:6px}
.s-l{font-size:12px;color:var(--muted);display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.s-v{font-family:'Fraunces',Georgia,serif;font-weight:600;font-size:26px;line-height:1;font-variant-numeric:tabular-nums}
.s-v small{font-family:'Noto Sans TC',sans-serif;font-size:12px;font-weight:400;color:var(--muted);margin-left:4px}
.cov{font-size:10px;padding:1px 6px;border-radius:999px;border:1px solid var(--border);color:var(--muted);font-weight:400;white-space:nowrap}
.cov.cc{border-color:color-mix(in srgb,var(--cc) 45%,transparent);color:var(--cc)}
.kv{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.kv li{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-top:1px solid var(--border);font-size:13.5px}
.kv li:first-child{border-top:none}.kv b{font-family:'IBM Plex Mono',monospace;font-weight:500}
.kv.small li{font-size:12.5px;padding:4px 0}
.split{display:flex;gap:24px;margin-bottom:8px}
.big{font-family:'Fraunces',Georgia,serif;font-size:30px;font-weight:600;line-height:1}.big.high{color:var(--high)}
.hchart,.lchart{display:flex;align-items:flex-end;gap:6px;height:130px}
.hcol,.lcol{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:3px;height:100%;min-width:0}
.hbar,.lbar{width:100%;max-width:40px;background:var(--accent-soft);border:1px solid var(--accent);border-radius:3px 3px 0 0;min-height:1px}
.hbar.peak{background:var(--accent)}
.hn,.ln{font-family:'IBM Plex Mono',monospace;font-size:10.5px}.hx,.lx{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted)}
.lead-s{font-size:15px;margin:0 0 8px}
.strip-wrap{overflow-x:auto}.strip-inner{min-width:560px}
.strip{position:relative;height:40px;background:var(--track);border-radius:6px}
.blk{position:absolute;top:0;bottom:0;background:var(--accent);border-radius:3px}
.brk{position:absolute;top:0;bottom:0;background:repeating-linear-gradient(45deg,transparent 0 5px,color-mix(in srgb,var(--muted) 24%,transparent) 5px 10px)}
.brk.soft{background:color-mix(in srgb,var(--accent) 22%,transparent)}
.brk-lbl{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted);background:var(--surface);padding:0 4px;border-radius:3px;white-space:nowrap}
.ticks{position:relative;height:18px;margin-top:4px}
.ticks span{position:absolute;font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:11.5px;color:var(--muted);margin-top:10px}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.sw.a{background:var(--accent)}.sw.b{background:repeating-linear-gradient(45deg,var(--track) 0 2px,var(--muted) 2px 4px)}
.sw.c{background:color-mix(in srgb,var(--accent) 22%,transparent)}
.ref{font-size:12.5px;color:var(--muted);margin:0;max-width:80ch;border-left:2px solid var(--border);padding-left:10px}
.note{font-size:12px;color:var(--muted);margin:8px 0 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;color:var(--muted);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid var(--border)}tr:last-child td{border-bottom:none}
.num{text-align:right;font-family:'IBM Plex Mono',monospace;font-variant-numeric:tabular-nums}
.tbl{overflow-x:auto}
details{border-top:1px solid var(--border);padding-top:10px}
summary{cursor:pointer;font-size:14px;font-weight:500;padding:4px 0}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
details .card{margin-top:10px}
.method p{font-size:13px;color:var(--muted);margin:0 0 10px;max-width:78ch}.method p:last-child{margin:0}
footer{font-size:11.5px;color:var(--muted);text-align:center}
"""
    page = f"""<title>Decision Pulse {int(out['date'][5:7])}/{int(out['date'][8:])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=Noto+Sans+TC:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{css}</style>
<div class="page">
<header><div class="eyebrow">Decision Pulse · {out['date']}（週{wd}）· 產生於 {e(out['generated_at'])} · {e(("職能：" + cfg['role']) if cfg['role'] else "尚未設定職能，使用通用預設")}</div>
<h1>{"今天" if out["is_today"] else f"{int(out['date'][5:7])}/{int(out['date'][8:])} "}用 AI 的認知負荷</h1></header>
<p class="lead">{e(summary_line(out))}</p>
{now_html}
{chain_html}
{sug_html}
{stage1}{stage2}{stage3}{stage4}{table}
<section>{details}</section>
<footer>decision-pulse · 只讀這台電腦上的 Claude Code 與 Codex 紀錄</footer>
</div>
"""
    # 查過去某天時，頁面上所有「今天」改成「這天」（建議文字、區塊標題、說明都共用同一套字串）
    return page if out["is_today"] else page.replace("今天", "這天")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--role", default=None, help="try another role's preset without editing config (read at import)")
    ap.add_argument("--html", default=None, help="write the dashboard page to this path")
    ap.add_argument("--json", action="store_true", help="also print the full JSON")
    ap.add_argument("--save", action="store_true",
                    help="write page and JSON to history/ under this date (one file per date, reused later)")
    ap.add_argument("--record-url", nargs=2, metavar=("DATE", "URL"),
                    help="remember where the page for DATE was published")
    args = ap.parse_args()

    def enc(o):
        return o.isoformat() if isinstance(o, datetime) else str(o)

    if args.record_url:
        date, url = args.record_url
        os.makedirs(HISTORY_DIR, exist_ok=True)
        published = json.load(open(PUBLISHED, encoding="utf-8")) if os.path.exists(PUBLISHED) else {}
        published[date] = url
        with open(PUBLISHED, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(published.items())), f, ensure_ascii=False, indent=2)
        print(f"recorded {date} → {url}")
        return

    date_str = args.date or datetime.now(TZ).date().isoformat()
    out = build(date_str)
    if args.save:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        args.html = os.path.join(HISTORY_DIR, f"decision-pulse-{date_str}.html")
        with open(os.path.join(HISTORY_DIR, f"{date_str}.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=enc)
    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(out))
        # The page is the product; printing the whole JSON would cost the reading
        # agent thousands of tokens every run.
        print(f"wrote {args.html}")
        print(summary_line(out))
        if not args.json:
            return

    print(json.dumps(out, ensure_ascii=False, indent=2, default=enc))


if __name__ == "__main__":
    main()
