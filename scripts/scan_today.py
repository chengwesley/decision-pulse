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
sys.stderr.reconfigure(encoding="utf-8")  # config errors (sys.exit) print Chinese too

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_LOCAL = os.path.join(SKILL_DIR, "config.local.json")
# 每天的結果存這裡（個人資料，不進版控）：decision-pulse-<日期>.html、<日期>.json、published.json（日期 → 發佈連結）
HISTORY_DIR = os.path.join(SKILL_DIR, "history")
PUBLISHED = os.path.join(HISTORY_DIR, "published.json")

# Defaults every user gets. Personal values (e.g. high-risk topics) are empty on
# purpose: the tool must produce a complete page for someone who configures nothing.
DEFAULTS = {
    "lang": "zh-TW",   # interface language: "zh-TW" or "en"
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
# Keywords and the advice example follow the interface language: someone who
# works in English writes English prompts.
ROLE_PRESETS = {
    "通用": {},
    "工程": {
        "density": [40, 70],
        "high_risk_keywords": {
            "zh-TW": ["production", "上線", "deploy", "migration", "資料庫", "權限", "secret"],
            "en": ["production", "go-live", "deploy", "migration", "database", "permission", "secret"]},
        "irreversible_extra": ["git reset --hard", "kubectl delete", "terraform apply", "migrate"],
    },
    "營運・HR": {
        "high_risk_keywords": {
            "zh-TW": ["調薪", "薪資", "離職", "資遣", "錄取", "申訴", "合約", "簽署", "撤權", "個資"],
            "en": ["pay raise", "salary", "resignation", "layoff", "offer letter", "grievance", "contract",
                   "signing", "revoke access", "personal data"]},
        "density_example": {"zh-TW": "這類詢問先照範本回，特殊的再來問我",
                            "en": "answer these requests from the template, and only check with me on the unusual ones"},
        "switching_is_the_job": True,
    },
    "業務": {
        "density": [20, 40],
        "high_risk_keywords": {
            "zh-TW": ["報價", "折扣", "合約", "簽約", "退款", "付款條件"],
            "en": ["quote", "discount", "contract", "sign the deal", "refund", "payment terms"]},
        "density_example": {"zh-TW": "報價照價目表出，要給折扣再問我",
                            "en": "quote from the price list, and ask me only before offering a discount"},
    },
    "設計": {
        "density": [20, 40],
        "high_risk_keywords": {"zh-TW": ["上線", "對外發布", "品牌"],
                               "en": ["go-live", "public release", "brand"]},
        "density_example": {"zh-TW": "沿用品牌規範的色票和字級，不用每次確認",
                            "en": "stick to the brand palette and type scale, no need to confirm each time"},
    },
    "主管": {
        "density": [15, 30],
        "high_risk_keywords": {
            "zh-TW": ["預算", "人事", "績效", "考核", "組織調整", "合約"],
            "en": ["budget", "headcount", "performance", "appraisal", "reorg", "contract"]},
        "density_example": {"zh-TW": "例行報表照上週的格式，不用每次問我",
                            "en": "keep the routine reports in last week's format, no need to ask me each time"},
        "switching_is_the_job": True,
    },
}
DEFAULT_EXAMPLE = {"zh-TW": "測試沒過就先修，不用問我", "en": "if the tests fail, just fix them, no need to ask me"}
ROLE_ALIASES = {"其他": "通用", "營運": "營運・HR", "HR": "營運・HR", "營運/HR": "營運・HR",
                "general": "通用", "other": "通用", "engineering": "工程", "engineer": "工程",
                "ops": "營運・HR", "operations": "營運・HR", "hr": "營運・HR", "ops-hr": "營運・HR",
                "sales": "業務", "design": "設計", "designer": "設計", "manager": "主管", "lead": "主管"}
ROLE_NAMES_EN = {"通用": "General", "工程": "Engineering", "營運・HR": "Operations / HR",
                 "業務": "Sales", "設計": "Design", "主管": "Manager"}
LANG_ALIASES = {"zh-TW": "zh-TW", "zh": "zh-TW", "zh-tw": "zh-TW", "tw": "zh-TW", "en": "en", "en-US": "en",
                "en-us": "en", "en-GB": "en", "english": "en"}


def cli_opt(name):
    """--role / --lang are read here, before argparse, because the whole config
    (and the module-level constants derived from it) is built at import time."""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def load_config():
    """Program defaults → role preset → the user's own config.local.json."""
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_LOCAL, encoding="utf-8") as f:
            local = json.load(f)
    except FileNotFoundError:
        local = {}
    except json.JSONDecodeError as exc:
        sys.exit(f"config.local.json 格式錯誤 / invalid JSON: {exc}")

    raw_lang = cli_opt("--lang") or local.get("lang") or cfg["lang"]
    lang = LANG_ALIASES.get(raw_lang, LANG_ALIASES.get(str(raw_lang).lower()))
    if lang is None:
        sys.exit(f"lang 應為 zh-TW 或 en / lang must be zh-TW or en, got: {raw_lang}")
    cfg["lang"] = lang
    en = lang == "en"
    cfg.update({"role": None, "density_example": DEFAULT_EXAMPLE[lang], "switching_is_the_job": False})

    role = cli_opt("--role") or local.get("role")
    role = ROLE_ALIASES.get(role, ROLE_ALIASES.get(str(role).lower(), role)) if role else role
    if role is not None and role not in ROLE_PRESETS:
        names = ", ".join(sorted({k for k, v in ROLE_ALIASES.items() if k.isascii()})) if en else "、".join(ROLE_PRESETS)
        sys.exit(f"role must be one of: {names}; got: {role}" if en
                 else f"role 應為 {names} 其中之一，收到：{role}")
    preset = ROLE_PRESETS.get(role or "通用", {})
    cfg["role"] = role
    if "density" in preset:
        cfg["risk_levels"]["density"] = preset["density"]
    if "high_risk_keywords" in preset:
        cfg["high_risk_keywords"] = {k: 3 for k in preset["high_risk_keywords"][lang]}
    cfg["irreversible_patterns"] = cfg["irreversible_patterns"] + preset.get("irreversible_extra", [])
    if "density_example" in preset:
        cfg["density_example"] = preset["density_example"][lang]
    if "switching_is_the_job" in preset:
        cfg["switching_is_the_job"] = preset["switching_is_the_job"]

    for k, v in local.items():
        if k.startswith("_") or k in ("role", "lang"):
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
        sys.exit(f"timezone 設定格式應為 +08:00 這種形式 / timezone must look like +08:00, got: {value}")
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


CFG = load_config()
LANG = CFG["lang"]
EN = LANG == "en"
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

STAGES = ([("density", "① Decision density"), ("recovery", "② Recovery window"),
           ("fatigue", "③ Signs of fatigue"), ("judgement", "④ Judgement quality")] if EN else
          [("density", "① 決策密度"), ("recovery", "② 恢復窗口"),
           ("fatigue", "③ 認知疲勞跡象"), ("judgement", "④ 判斷品質")])
# Levels, reasons, notes and verification signals stay as these Chinese codes in
# the JSON whatever the interface language, so pulse-clock and saved history/
# files keep working; they're translated only when shown.
LEVEL_ORDER = {"低": 0, "中": 1, "高": 2}

# Fixed text triggered by rules — never generated on the fly, so days stay
# comparable. Each one starts from a cognitive-science mechanism, and each was
# checked for (1) being doable in a one-person, turn-by-turn chat with an AI and
# (2) not contradicting another stage's advice. Ego depletion / "willpower runs
# out" is deliberately absent: it failed large preregistered replications.
SUGGESTIONS = [
    {"stage": "density", "when": {"高"},
     "text": "今天很多小事都跑來等你拍板。可以先幫常見情況定好規則，像「{example}」，AI 照著走，你的腦袋就能留給真正要想的事。",
     "why": "事先想好「如果…就…」，當下就不必再判斷一次", "cite": "執行意圖，Gollwitzer & Sheeran 2006 後設分析（信心高）",
     "en": {"text": "A lot of small things were waiting on your call today. It might help to set a few rules ahead of time for the usual cases, like “{example}”, so the AI can follow them and your head stays free for what really needs thought.",
            "why": "Deciding “if this happens, I’ll do that” in advance means you don’t have to decide again in the moment",
            "cite": "Implementation intentions, Gollwitzer & Sheeran 2006 meta-analysis (confidence: high)"}},
    {"stage": "density", "when": {"中", "高"},
     "text": "一開始把要求講清楚：範圍、限制、做到哪裡算完成。AI 中途來問你的次數會少很多。",
     "why": "目標越具體，表現越好", "cite": "目標設定理論，Locke & Latham 2002（信心中高）",
     "en": {"text": "When you start a task, try spelling out the scope, the limits, and what “done” looks like. The AI will likely come back to ask you far less often.",
            "why": "The more specific the goal, the better the performance",
            "cite": "Goal-setting theory, Locke & Latham 2002 (confidence: medium-high)"}},
    {"stage": "recovery", "when": {"中", "高"},
     "text": "已經連續好一陣子沒停了。下一件事開始前，起來走走、看看窗外，離開螢幕十分鐘再回來。",
     "why": "十分鐘以上的休息才有恢復效果；看遠處、看綠色有助於恢復專注力",
     "cite": "Albulescu et al. 2022（信心中高）；注意力恢復理論，Kaplan 1995（信心中）",
     "en": {"text": "You’ve been going for quite a while without a pause. Before the next thing, maybe get up, walk around, look out the window, and give yourself ten minutes away from the screen.",
            "why": "Breaks of ten minutes or more are what actually help you recover; looking into the distance or at something green helps your attention come back",
            "cite": "Albulescu et al. 2022 (confidence: medium-high); attention restoration theory, Kaplan 1995 (confidence: medium)"}},
    {"stage": "recovery", "when": {"高"},
     "text": "明天試著幫自己留一兩段不被打擾的時間，給需要專心想的事。",
     "why": "常被打斷的人會加快速度補回時間，代價是壓力和挫折感上升", "cite": "Mark, Gudith & Klocke 2008（信心中高）",
     "en": {"text": "Tomorrow, see if you can keep one or two stretches free of interruptions for the work that needs real focus.",
            "why": "People who get interrupted often work faster to catch up, and pay for it with more stress and frustration",
            "cite": "Mark, Gudith & Klocke 2008 (confidence: medium-high)"}},
    {"stage": "fatigue", "when": {"中", "高"},
     "text": "要切去別的對話之前，花半分鐘寫下「做到哪、下一步是什麼」。回來接得上，離開時心裡也比較放得下。",
     "why": "切換前寫下接續計畫，注意力比較不會卡在上一件事", "cite": "接續計畫，Leroy & Glomb 2018（信心中高）",
     "en": {"text": "Before you jump to another conversation, take half a minute to jot down where you are and what comes next. It’s easier to pick up again, and easier to let go when you leave.",
            "why": "Writing a plan for picking back up keeps your attention from getting stuck on the last task",
            "cite": "Ready-to-resume plans, Leroy & Glomb 2018 (confidence: medium-high)"}},
    {"stage": "fatigue", "when": {"高"}, "skip_if_switching_is_the_job": True,
     "text": "同時開著的對話有點多，先收掉幾個，留兩個就好。",
     "why": "大腦同時只能抓住大約四件事，每個開著的對話都在佔位子",
     "cite": "工作記憶容量，Cowan 2001（信心中高）；「兩個」是經驗值",
     "en": {"text": "There are quite a few conversations open at once. Maybe close a few and keep just two going.",
            "why": "Your brain can hold only about four things at a time, and every open conversation takes up a slot",
            "cite": "Working memory capacity, Cowan 2001 (confidence: medium-high); “two” is a rule of thumb"}},
    {"stage": "judgement", "when": {"中", "高"},
     "text": "AI 寫了一大段的時候，給自己一點時間讀完再回。可以問自己：「要跟同事解釋的話，我講得出為什麼同意嗎？」",
     "why": "知道要為決定負責時，比較不會照單全收機器的建議", "cite": "問責效應，Skitka, Mosier & Burdick 2000（信心中）",
     "en": {"text": "When the AI writes a long answer, give yourself a moment to read it before you reply. One question that helps: “If I had to explain this to a colleague, could I say why I agreed?”",
            "why": "When people know they’ll have to justify a decision, they’re less likely to take a machine’s suggestion as is",
            "cite": "Accountability effect, Skitka, Mosier & Burdick 2000 (confidence: medium)"}},
    {"stage": "judgement", "when": {"topic_unverified"},
     "text": "碰到比較重大的決定，請 AI 反過來列出「這樣做可能哪裡不對」，看完再決定。",
     "why": "刻意想想反面，是少數被重複驗證有效的去偏誤方法", "cite": "考慮反面，Lord, Lepper & Preston 1984（信心中高）",
     "en": {"text": "For the bigger decisions, try asking the AI to list what could go wrong with the plan, and read that before you decide.",
            "why": "Deliberately considering the opposite is one of the few debiasing methods that has held up again and again",
            "cite": "Consider-the-opposite, Lord, Lepper & Preston 1984 (confidence: medium-high)"}},
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
        return "~ (home)" if EN else "~（家目錄）"
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
                words = sug["en"] if EN else sug
                suggestions.append({"stage": sug["stage"], "why": words["why"], "cite": words["cite"],
                                    "text": words["text"].format(example=CFG["density_example"]),
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
        "config": {"lang": CFG["lang"], "role": CFG["role"], "personal_topics": len(CFG["high_risk_keywords"]),
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

# Every word the page shows, in both interface languages. The JSON keeps the
# Chinese codes (levels, reasons, notes, signals); these tables translate them.
UI = {
    "zh-TW": {
        "list_sep": "、", "sep": "；", "weekdays": "一二三四五六日",
        "both": "兩者", "cc_only": "僅 Claude Code",
        "levels": {"低": "低", "中": "中", "高": "高", "追加": "追加"},
        "notes": {}, "reasons": {}, "signals": {}, "segments": {},
        "actions": {"ask": "AI 停下來要你決定", "stopped": "你中止 AI 的動作", "irreversible": "不可逆指令"},
        "cant_tell": "無法判斷",
        # summary
        "no_usage": "{date} 沒有使用 AI 的紀錄。",
        "peak": "對話最密集在 {h}:00–{h2}:00（{n} 則）",
        "stage_names": {"density": "決策密度", "recovery": "恢復窗口", "fatigue": "疲勞跡象", "judgement": "判斷品質"},
        "graded_item": "{name}{level}",
        "day_today": "今天", "day_past": "這天",
        "summary": "{day} {conv} 段對話、實際對話 {active} 分鐘；{peak}；最長一段沒休息 {longest} 分鐘。四個階段：{graded}。",
        # chain
        "units": {"density": "則／活躍小時", "recovery": "分鐘沒有 10 分鐘休息", "fatigue": "次切換／活躍小時",
                  "judgement": "長內容秒回（{a}／{b}）"},
        "basis_mixed": "門檻：研究＋經驗值", "basis_rule": "門檻：經驗值",
        "flow_label": "四階段風險分級",
        "legend": "直線＝今天的數值落點；四個階段各自分級，不加總",
        # care
        "care_h": "今天想提醒你的幾件事",
        "care_sub": "辛苦了。下面是從今天的節奏裡看到、可以試試看的小調整。",
        "care_ok": "今天的節奏還不錯，繼續保持。",
        # now
        "now_block": "這一段已連續 <b>{m} 分鐘</b>",
        "now_switches": "最近 {w} 分鐘切換 <b>{n}</b> 次",
        "now_last_break": "上次 {i} 分鐘以上的休息：{s}–{e}",
        "now_no_break": "今天還沒有 10 分鐘以上的休息",
        "now_over": "距離上次 {i} 分鐘以上的休息已經 {m} 分鐘。",
        "now_idle": "已經 {m} 分鐘沒有對話",
        "now_at": "此刻 {t}",
        # stage 1
        "spike_off": "（當天回覆少於 20 次，此規則未啟用）",
        "fatigue_note": "（所在活動段已連續 ≥ {m} 分鐘）",
        "no_keywords": "（未設定關鍵字）",
        "late_tag": "（深夜）", "late_code_name": "深夜",
        "irr_detail": "（{items}）",
        "spike_note": "資訊量爆量＝你發這則之前 AI 累積寫的字超過當天第 90 百分位（今天是 {n} 字）。",
        "s1_h": "決策密度", "s1_q": "AI 讓決策密度暴增——今天有多少事經過你的判斷？",
        "st_conv": ("對話段數", "段"), "st_msgs": ("你發的訊息", "則"), "st_typed": ("你打的字", "字"),
        "st_ai_chars": ("AI 回的字", "字"), "st_tokens": ("資訊量 token", ""), "st_thinking": ("思考量 thinking token", ""),
        "events_h": "決策事件", "ev_asked": "AI 停下來要你決定", "ev_changed": "你改變主意（排了指令又取消）",
        "ev_stopped": "你中止 AI 的動作",
        "hi_vs_h": "一般 vs 高度重要", "hi_msgs": "高度重要訊息（佔 {p}）", "normal_msgs": "一般訊息",
        "hi_actions": "高度重要動作", "multi_rule": "一則訊息可能同時符合多條規則。",
        "seg_h": "早中晚", "seg_th": ("時段", "時間", "訊息", "你打的字"),
        "hourly_h": "每小時訊息量", "hourly_note": "深色＝最密集的一小時。",
        # stage 2
        "brk_lbl": "{m} 分", "brk_title": "{s}→{e}，{m} 分鐘沒有輸入", "blk_title": "{s}–{e}，{m} 分鐘",
        "strip_legend": ("實際在對話", "真的離開", "沒輸入但 AI 還在跑"),
        "s2_h": "恢復窗口", "s2_q": "恢復窗口消失——今天有沒有真的停下來？",
        "st_active": ("實際對話時間", "分"), "st_longest": ("最長一段沒休息", "分"),
        "st_away": ("真正離開", "次"), "st_longest_break": ("最長一次休息", "分"),
        "s2_ref": "「真正離開」＝連續 {i} 分鐘以上沒有你的輸入、AI 也沒在跑。{i} 分鐘對齊 Albulescu et al. (2022)：不到 10 分鐘的休息不足以讓高認知需求的工作恢復（信心中高）。持續高強度認知控制約 {h} 小時後前額葉出現代價（Blain et al. 2016，借用的實驗室門檻，信心中）——今天{over}超過。",
        "over_yes": "已", "over_no": "未",
        # stage 3
        "lx_msgs": "{n} 則",
        "s3_h": "認知疲勞的跡象", "s3_q": "注意力被切碎、指令變短、深夜還在用？",
        "st_switches": ("切換", "次"), "st_returns": ("其中跳回先前丟下的對話", "次"),
        "st_sw_density": ("切換密度", "次／活躍小時"), "st_concurrent": ("同時開著", "段 @ {t}"),
        "storm_h": "最密集的 {w} 分鐘", "storm": "{s}–{e}：{n} 次切換，其中 {r} 次跳回", "storm_none": "今天沒有切換密集的時窗",
        "focus_run": "最長不切換連續段", "focus_run_v": "{n} 則", "late_msgs": "深夜訊息",
        "len_h": "指令長度 · 每段活動的中位數字數",
        "len_note": "過載時指令傾向變短、變含糊。全天中位數 {n} 字。",
        "s3_ref": "「跳回」＝切到今天稍早用過、中途離開的對話，最接近注意力殘留研究說的未閉合切換（Leroy 2009；Mark et al. 2008，信心中高）。切換以對話為單位，同一個視窗裡換資料夾不算。",
        # stage 4
        "no_signal": "無",
        "s4_h": "判斷品質", "s4_q": "判斷品質下滑——AI 講完你有沒有真的看？",
        "st_fast": ("長內容後秒回", "%"), "st_long": ("長內容回覆次數", "次"),
        "st_reply": ("回覆延遲中位數", "秒"), "st_retry": ("錯誤後重試", "／{n} 次錯誤"),
        "verify_h": "高度重要訊息所在對話的驗證行為", "verify_th": ("專案", "高度重要訊息", "驗證訊號"),
        "verify_none": "沒有 Claude Code 的高度重要訊息。",
        "work_h": "工作訊號（信心低中）", "explore": "探索／產出比（讀、查 vs 寫、改）", "agents": "派 subagent",
        "work_note": "多讀少寫可能是謹慎，也可能只是任務本身要查很多。",
        "s4_ref": "「長內容後秒回」＝AI 自你上次發言後寫了 {c} 字以上，你 {s} 秒內就回覆（自動化自滿，Parasuraman &amp; Manzey 2010，信心中）。這一階段只看做判斷時的條件，不判斷決定對不對——對話紀錄沒有結果資料。",
        # block table
        "table_h": "每段活動的條件並排",
        "table_th": ("時間", "時段", "長度（分）", "之前休息（分）", "訊息", "切換密度", "指令長度", "長內容秒回", "高度重要", "工具"),
        "table_note": "只列長度 ≥ {m} 分鐘且 ≥ {n} 則訊息的活動段。「長內容秒回」只有 Claude Code 有資料，沒有就顯示「—」。<b>單日只有幾段、時段又跟做什麼工作疊在一起，這張表只把條件並排給你看，無法回答「越晚是否越差」。</b>",
        # details
        "sessions_h": "對話明細（{n} 段）",
        "sessions_th": ("時間", "工具", "專案", "你發話", "你打的字", "AI 回的字", "工具呼叫"),
        "method_h": "方法與限制",
        "method": (
            "資料只來自這台電腦上的 Claude Code 與 Codex 對話紀錄。同一段對話被續寫、分叉或換資料夾時會被複製成新檔案，已合併去重（合併 {merged} 個檔案、去掉 {dups} 筆重複事件）。Codex 的背景自動審查與子代理（{bg} 條）不算你的使用。",
            "只算你真正打的字：系統自動插入的內容（例如 system-reminder、排程任務、slash command 輸出）會先剝除。",
            "Codex 的紀錄沒有 AI 回覆字數、token 與回覆延遲，標「僅 Claude Code」的指標只反映 Claude Code 的使用。",
            "「認知負荷」刻意不做成單一分數——恢復窗口、疲勞跡象、判斷品質是不同的東西，加起來會製造假精確。",
            "門檻：{i} 分鐘有效休息（Albulescu et al. 2022）、{h} 小時持續控制（Blain et al. 2016，借用）有文獻依據；疲勞情境 {f} 分鐘、長內容 {c} 字、秒回 {s} 秒是工程慣例，可在 config.local.json 調整。",
            "刻意不做：跨日比較、單一總分、判斷決策對不對、情緒推論、AI 生成的建議句、主動推播。",
        ),
        # header / footer
        "eyebrow": "Decision Pulse · {date}（週{wd}）· 產生於 {gen} · {role}",
        "role": "職能：{r}", "no_role": "尚未設定職能，使用通用預設",
        "h1_today": "今天用 AI 的認知負荷", "h1_past": "{m}/{d} 用 AI 的認知負荷",
        "footer": "decision-pulse · 只讀這台電腦上的 Claude Code 與 Codex 紀錄",
        "past_swaps": (("今天", "這天"),),
    },
    "en": {
        "list_sep": ", ", "sep": "; ", "weekdays": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        "both": "both", "cc_only": "Claude Code only",
        "levels": {"低": "low", "中": "medium", "高": "high", "追加": "also"},
        "notes": {"樣本不足": "not enough data", "無法判斷": "can't tell", "有深夜使用": "late-night use",
                  "高風險主題沒有驗證": "high-stakes topic not double-checked",
                  "超過 {h} 小時": "over {h} hours"},
        "reasons": {"資訊量爆量": "Information spike", "深夜": "Late night", "疲勞情境": "While already tired",
                    "高風險主題": "High-stakes topic"},
        "signals": {"派 subagent": "sent a subagent", "review 類 skill": "review skill",
                    "查外部來源": "checked outside sources"},
        "segments": {"早": "Morning", "中": "Afternoon", "晚": "Evening", "深夜": "Late night"},
        "actions": {"ask": "The AI stopped to ask you", "stopped": "You stopped the AI",
                    "irreversible": "Irreversible commands"},
        "cant_tell": "can't tell",
        "no_usage": "No AI usage recorded on {date}.",
        "peak": "busiest hour {h}:00–{h2}:00 ({n} messages)",
        "stage_names": {"density": "decision density", "recovery": "recovery window", "fatigue": "fatigue signs",
                        "judgement": "judgement quality"},
        "graded_item": "{name} {level}",
        "day_today": "Today", "day_past": "That day",
        "summary": "{day}: {conv} conversations, {active} min in conversation; {peak}; longest stretch without a break {longest} min. Four stages: {graded}.",
        "units": {"density": "messages / active hour", "recovery": "min without a 10-min break",
                  "fatigue": "switches / active hour", "judgement": "quick replies to long outputs ({a}/{b})"},
        "basis_mixed": "Threshold: research + rule of thumb", "basis_rule": "Threshold: rule of thumb",
        "flow_label": "Four-stage risk levels",
        "legend": "Line = where today's value falls; each stage is graded on its own, never summed",
        "care_h": "A few things worth noticing today",
        "care_sub": "You've put in a lot today. Here are a few small adjustments you could try, based on how today went.",
        "care_ok": "Today's pace looks good. Keep it up.",
        "now_block": "Current stretch: <b>{m} min</b>",
        "now_switches": "<b>{n}</b> switches in the last {w} min",
        "now_last_break": "last break of {i}+ min: {s}–{e}",
        "now_no_break": "no break of 10+ min yet today",
        "now_over": "It has been {m} min since your last break of {i}+ min.",
        "now_idle": "No conversation for {m} min",
        "now_at": "Now {t}",
        "spike_off": " (fewer than 20 replies today, rule off)",
        "fatigue_note": " (block already ≥ {m} min)",
        "no_keywords": " (no keywords set)",
        "late_tag": " (late night)", "late_code_name": "深夜",
        "irr_detail": " ({items})",
        "spike_note": "Information spike = before you sent it, the AI had written more than the day's 90th percentile (today: {n} characters). ",
        "s1_h": "Decision density", "s1_q": "AI multiplies the decisions you make. How many went through you today?",
        "st_conv": ("Conversations", ""), "st_msgs": ("Messages you sent", ""), "st_typed": ("Characters you typed", ""),
        "st_ai_chars": ("Characters the AI wrote", ""), "st_tokens": ("Tokens", ""),
        "st_thinking": ("Thinking tokens", ""),
        "events_h": "Decision events", "ev_asked": "The AI stopped to ask you",
        "ev_changed": "You changed your mind (queued, then cancelled)", "ev_stopped": "You stopped the AI",
        "hi_vs_h": "Routine vs high-importance", "hi_msgs": "High-importance messages ({p})",
        "normal_msgs": "Routine messages", "hi_actions": "High-importance actions",
        "multi_rule": "One message can match more than one rule.",
        "seg_h": "Time of day", "seg_th": ("Part of day", "Hours", "Messages", "Characters typed"),
        "hourly_h": "Messages per hour", "hourly_note": "Dark = the busiest hour.",
        "brk_lbl": "{m} min", "brk_title": "{s}→{e}, {m} min without input", "blk_title": "{s}–{e}, {m} min",
        "strip_legend": ("In conversation", "Really away", "No input, AI still working"),
        "s2_h": "Recovery window", "s2_q": "Recovery windows disappear. Did you actually stop today?",
        "st_active": ("Time in conversation", "min"), "st_longest": ("Longest stretch without a break", "min"),
        "st_away": ("Times really away", ""), "st_longest_break": ("Longest break", "min"),
        "s2_ref": "“Really away” = {i}+ minutes with no input from you and no AI activity. {i} minutes follows Albulescu et al. (2022): breaks shorter than 10 minutes don't restore you after demanding work (confidence: medium-high). After about {h} hours of sustained, demanding cognitive control the prefrontal cortex shows a cost (Blain et al. 2016, a borrowed lab threshold, confidence: medium). Today this was {over}.",
        "over_yes": "exceeded", "over_no": "not exceeded",
        "lx_msgs": "{n} msgs",
        "s3_h": "Signs of fatigue", "s3_q": "Is attention breaking into pieces, are prompts getting shorter, still going late at night?",
        "st_switches": ("Switches", ""), "st_returns": ("Returns to a conversation left earlier", ""),
        "st_sw_density": ("Switch density", "/ active hour"), "st_concurrent": ("Open at once", "@ {t}"),
        "storm_h": "Busiest {w} minutes", "storm": "{s}–{e}: {n} switches, {r} of them returns",
        "storm_none": "No dense switching window today",
        "focus_run": "Longest run without switching", "focus_run_v": "{n} msgs", "late_msgs": "Late-night messages",
        "len_h": "Prompt length · median characters per block",
        "len_note": "Under overload, prompts tend to get shorter and vaguer. Median for the day: {n} characters.",
        "s3_ref": "“Return” = switching back to a conversation you used and left earlier today, the closest match to the unfinished switches studied in attention-residue research (Leroy 2009; Mark et al. 2008, confidence: medium-high). Switching is counted between conversations; changing folders inside one window doesn't count.",
        "no_signal": "none",
        "s4_h": "Judgement quality", "s4_q": "Judgement slips. Did you really read what the AI said?",
        "st_fast": ("Quick replies to long outputs", "%"), "st_long": ("Long outputs you replied to", ""),
        "st_reply": ("Median reply time", "s"), "st_retry": ("Retries after an error", "/ {n} errors"),
        "verify_h": "Checking behaviour in conversations with high-importance messages",
        "verify_th": ("Project", "High-importance", "Checks"),
        "verify_none": "No high-importance messages in Claude Code.",
        "work_h": "Work signals (confidence: low-medium)",
        "explore": "Explore / produce ratio (reading, searching vs writing, editing)", "agents": "Subagents sent",
        "work_note": "Reading more than writing can mean care, or just a task that needs a lot of looking up.",
        "s4_ref": "“Quick reply to a long output” = since your last message the AI wrote {c}+ characters and you replied within {s} seconds (automation complacency, Parasuraman &amp; Manzey 2010, confidence: medium). This stage looks only at the conditions you decided under, not whether the decision was right. Transcripts don't contain outcomes.",
        "table_h": "Activity blocks side by side",
        "table_th": ("Time", "Part of day", "Length (min)", "Break before (min)", "Messages", "Switch density",
                     "Prompt length", "Quick replies to long outputs", "High-importance", "Tools"),
        "table_note": "Only blocks of at least {m} minutes and {n} messages are listed. “Quick replies to long outputs” has data for Claude Code only; otherwise it shows “—”. <b>With only a few blocks in a day, and time of day tangled up with the kind of work you were doing, this table only lines the conditions up. It can't tell you whether later means worse.</b>",
        "sessions_h": "Conversation details ({n})",
        "sessions_th": ("Time", "Tool", "Project", "Your messages", "Characters typed", "AI characters", "Tool calls"),
        "method_h": "Method and limitations",
        "method": (
            "Data comes only from the Claude Code and Codex transcripts on this computer. When a conversation is resumed, forked or moved to another folder it gets copied into a new file; these are merged and deduplicated ({merged} files merged, {dups} duplicate events removed). Codex background reviews and subagents ({bg} threads) don't count as your use.",
            "Only what you actually typed counts: content the system inserts automatically (system reminders, scheduled tasks, slash command output) is stripped first.",
            "Codex transcripts have no AI reply length, tokens or reply timing, so metrics marked “Claude Code only” reflect Claude Code use only.",
            "“Cognitive load” is deliberately not a single score. Recovery, fatigue and judgement are different things, and adding them up would only look precise.",
            "Thresholds: the {i}-minute effective break (Albulescu et al. 2022) and {h} hours of sustained control (Blain et al. 2016, borrowed) come from research; the {f}-minute fatigue context, {c}-character long output and {s}-second quick reply are engineering conventions you can change in config.local.json.",
            "Deliberately not done: comparisons across days, a single total score, judging whether decisions were right, guessing emotions, AI-written suggestions, push reminders.",
        ),
        "eyebrow": "Decision Pulse · {date} ({wd}) · generated {gen} · {role}",
        "role": "Role: {r}", "no_role": "No role set, using general defaults",
        "h1_today": "Today's cognitive load with AI", "h1_past": "Cognitive load with AI on {m}/{d}",
        "footer": "decision-pulse · reads only the Claude Code and Codex transcripts on this computer",
        "past_swaps": (("Today's", "That day's"), ("today's", "that day's"), ("Today", "That day"), ("today", "that day")),
    },
}
L = UI[LANG]


def tr(table, code):
    """Show a Chinese code (level, reason, note, signal) in the interface language."""
    if not EN:
        return code
    m = re.fullmatch(r"超過 (\d+) 小時", code or "")
    if table == "notes" and m:
        return L["notes"]["超過 {h} 小時"].format(h=m.group(1))
    return L[table].get(code, code)


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
        return L["no_usage"].format(date=out["date"])
    peak = (L["peak"].format(h=d1["peak_hour"], h2=f"{int(d1['peak_hour']) + 1:02d}", n=d1["peak_hour_messages"])
            if d1["peak_hour"] else "")
    graded = L["list_sep"].join(L["graded_item"].format(name=L["stage_names"][k],
                                                         level=tr("levels", v["level"]) if v["level"] else L["cant_tell"])
                                for k, v in out["levels"].items())
    day = L["day_today"] if out["is_today"] else L["day_past"]
    return L["summary"].format(day=day, conv=d1["conversations"], active=_f(d2["active_min"]), peak=peak,
                               longest=_f(d2["longest_block_min"]), graded=graded)


def render_html(out):
    e = html.escape
    d1, d2, d3, d4 = out["stage1_density"], out["stage2_recovery"], out["stage3_fatigue"], out["stage4_judgement"]
    cfg = out["config"]
    date = datetime.fromisoformat(out["date"])
    wd = L["weekdays"][date.weekday()]
    both, cc = f'<span class="cov">{L["both"]}</span>', f'<span class="cov cc">{L["cc_only"]}</span>'
    sep = L["list_sep"]

    def stat(label, value, unit="", cov=""):
        return (f'<div class="stat"><div class="s-l">{e(label)} {cov}</div>'
                f'<div class="s-v">{value}<small>{e(unit)}</small></div></div>')

    def st(key, value, cov="", **kw):
        label, unit = L[key]
        return stat(label, value, unit.format(**kw), cov)

    def notes_of(lv):
        return sep.join(tr("notes", n) for n in lv["notes"])

    # ---- graded chain: the mechanism as a flow, each stage coloured by its own level
    ratio = d4["fast_long_ratio"]
    lvls = out["levels"]
    cls_of = {"低": "lo", "中": "mi", "高": "hi"}
    units = dict(L["units"])
    units["judgement"] = units["judgement"].format(a=d4["fast_long"], b=d4["long_replies"])
    nodes = []
    for n_i, (key, title) in enumerate(STAGES, 1):
        lv = lvls[key]
        lo, hi = lv["thresholds"]
        pct = key == "judgement"
        v = lv["value"]
        shown = (f"{v * 100:.0f}%" if pct else _f(v)) if v is not None else "—"
        tag = tr("levels", lv["level"]) if lv["level"] else (notes_of(lv) or L["cant_tell"])
        scale = ""
        if v is not None:
            vmax = hi * 1.6
            pos = lambda x: max(0.0, min(100.0, x / vmax * 100))  # noqa: E731
            scale = (f'<div class="rs"><span class="rs-lo" style="width:{pos(lo):.1f}%"></span>'
                     f'<span class="rs-mi" style="width:{pos(hi) - pos(lo):.1f}%"></span><span class="rs-hi"></span>'
                     f'<i class="rs-pin" style="left:{pos(v):.1f}%"></i></div>'
                     f'<div class="rs-cap"><span style="left:{pos(lo):.1f}%">{f"{lo * 100:.0f}%" if pct else _f(lo)}</span>'
                     f'<span style="left:{pos(hi):.1f}%">{f"{hi * 100:.0f}%" if pct else _f(hi)}</span></div>'
                     f'<div class="rs-basis">{L["basis_mixed"] if key == "recovery" else L["basis_rule"]}</div>')
        extra = f'<div class="rn">{e(notes_of(lv))}</div>' if lv["level"] and lv["notes"] else ""
        nodes.append(
            f'<a class="node {cls_of.get(lv["level"], "na")}" href="#s{n_i}"><span class="n-t">{e(title)}</span>'
            f'<span class="n-row"><span class="n-v">{shown}</span><span class="n-tag">{e(tag)}</span></span>'
            f'<span class="n-u">{e(units[key])}</span>{scale}{extra}</a>')
    lv_name = L["levels"]
    chain_html = (f'<nav class="flow" aria-label="{L["flow_label"]}">'
                  + '<span class="arrow" aria-hidden="true">→</span>'.join(nodes) + "</nav>"
                  + f'<div class="legend"><span><i class="sw lo"></i>{lv_name["低"]}</span><span><i class="sw mi"></i>{lv_name["中"]}</span>'
                    f'<span><i class="sw hi"></i>{lv_name["高"]}</span><span>{L["legend"]}</span></div>')

    # ---- rule-triggered suggestions, in a caring voice (fixed text, never generated)
    sugs = out["suggestions"]
    if sugs:
        items = "".join(
            f'<li><span class="s-tag {cls_of.get(s["level"], "mi")}">{e(dict(STAGES)[s["stage"]][:1])} {e(tr("levels", s["level"]))}</span>'
            f'<div><div class="s-text">{e(s["text"])}</div>'
            f'<div class="s-why">{e(s["why"])} · {e(s["cite"])}</div></div></li>' for s in sugs)
        sug_html = (f'<section class="care"><h2 class="care-h">{L["care_h"]}</h2>'
                    f'<p class="care-sub">{L["care_sub"]}</p>'
                    f'<ul class="sugs">{items}</ul></section>')
    else:
        sug_html = f'<section class="care"><h2 class="care-h">{L["care_ok"]}</h2></section>'

    # ---- now
    now_html = ""
    n = out.get("now")
    if n:
        if n["active_now"]:
            lb = n["last_real_break"]
            parts = [L["now_block"].format(m=_f(n["current_block_min"])),
                     L["now_switches"].format(w=WINDOW_MIN, n=n["switches_last_window"]),
                     L["now_last_break"].format(i=IDLE_MIN, s=lb["start"], e=lb["end"]) if lb else L["now_no_break"]]
            txt = L["sep"].join(parts)
            if n["over_fatigue"]:
                txt += f'<div class="now-fact">{L["now_over"].format(i=IDLE_MIN, m=_f(n["current_block_min"]))}</div>'
        else:
            txt = L["now_idle"].format(m=_f(n["minutes_since_last_message"]))
        now_html = f'<section class="now"><span class="now-at">{e(L["now_at"].format(t=n["at"]))}</span><div>{txt}</div></section>'

    # ---- stage 1
    high_pct = f"{d1['high_messages'] / d1['human_messages'] * 100:.0f}%" if d1["human_messages"] else "—"
    reason_chips = []
    for r, note in (("資訊量爆量", "" if d1["spike_rule_on"] else L["spike_off"]),
                    ("深夜", ""), ("疲勞情境", L["fatigue_note"].format(m=cfg["fatigue_block_min"])),
                    ("高風險主題", "" if cfg["personal_topics"] else L["no_keywords"])):
        reason_chips.append(f'<li><span>{e(tr("reasons", r))}{e(note)}</span><b>{d1["reason_counts"].get(r, 0)}</b></li>')
    act = d1["actions"]
    irr = d1["irreversible_detail"]
    irr_txt = L["irr_detail"].format(items=sep.join(f'{k} {v}' for k, v in sorted(irr.items(), key=lambda kv: -kv[1]))) if irr else ""
    action_rows = "".join(
        f'<li><span>{e(L["actions"][k])}{e(irr_txt) if k == "irreversible" else ""}</span><b>{act.get(k, 0)}</b></li>'
        for k in L["actions"])
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
        f'<tr><td>{e(tr("segments", s["name"]))}{L["late_tag"] if s["late_night"] and s["name"] != L["late_code_name"] else ""}</td>'
        f'<td class="num">{s["range"]}</td><td class="num">{s["messages"]}</td><td class="num">{_f(s["chars"])}</td></tr>'
        for s in d1["segments"])
    spike_note = L["spike_note"].format(n=_f(d1["spike_threshold_chars"])) if d1["spike_rule_on"] else ""
    th = L["seg_th"]

    stage1 = f"""
<section id="s1" class="stage"><div class="st-h"><span class="st-n">1</span><div><h2>{L["s1_h"]}</h2>
<p class="st-q">{L["s1_q"]}</p></div></div>
<div class="grid6">{st("st_conv", d1['conversations'], both)}{st("st_msgs", _f(d1['human_messages']), both)}
{st("st_typed", _f(d1['human_chars']), both)}{st("st_ai_chars", _f(d1['assistant_chars']), cc)}
{st("st_tokens", _f(d1['tokens']), cc)}{st("st_thinking", _f(d1['thinking_tokens']), cc)}</div>
<div class="two">
<div class="card"><h3>{L["events_h"]} {cc}</h3><ul class="kv">
<li><span>{L["ev_asked"]}</span><b>{d1['ai_asked_you']}</b></li>
<li><span>{L["ev_changed"]}</span><b>{d1['changed_mind']}</b></li>
<li><span>{L["ev_stopped"]}</span><b>{d1['stopped_ai']}</b></li></ul></div>
<div class="card"><h3>{L["hi_vs_h"]}</h3>
<div class="split"><div><div class="big high">{d1['high_messages']}</div><div class="s-l">{L["hi_msgs"].format(p=high_pct)}{both}</div></div>
<div><div class="big">{d1['human_messages'] - d1['high_messages']}</div><div class="s-l">{L["normal_msgs"]}</div></div></div>
<ul class="kv small">{"".join(reason_chips)}</ul>
<h4>{L["hi_actions"]} {cc}</h4><ul class="kv small">{action_rows}</ul>
<p class="note">{e(spike_note)}{L["multi_rule"]}</p></div></div>
<div class="two">
<div class="card"><h3>{L["seg_h"]} {both}</h3><table><tr><th>{th[0]}</th><th class="num">{th[1]}</th><th class="num">{th[2]}</th><th class="num">{th[3]}</th></tr>{seg_rows}</table></div>
<div class="card"><h3>{L["hourly_h"]} {both}</h3><div class="hchart">{hbars}</div>
<p class="note">{L["hourly_note"]}</p></div></div>
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
            lbl = f'<span class="brk-lbl">{L["brk_lbl"].format(m=_f(b["minutes"], 0))}</span>' if b["minutes"] >= 30 else ""
            segs.append(f'<div class="{cls}" style="left:{x(b["start"]):.2f}%;width:{x(b["end"]) - x(b["start"]):.2f}%" '
                        f'title="{L["brk_title"].format(s=b["start"], e=b["end"], m=_f(b["minutes"]))}">{lbl}</div>')
        for b in d2["blocks"]:
            segs.append(f'<div class="blk" style="left:{x(b["start"]):.2f}%;width:{max(x(b["end"]) - x(b["start"]), 0.4):.2f}%" '
                        f'title="{L["blk_title"].format(s=b["start"], e=b["end"], m=_f(b["minutes"]))}"></div>')
        hrs = list(range(t0, t1 + 1, 60))
        ticks = "".join(f'<span style="left:{(h - t0) / span * 100:.2f}%;transform:translateX('
                        f'{"0" if j == 0 else "-100%" if j == len(hrs) - 1 else "-50%"})">{h // 60:02d}:00</span>'
                        for j, h in enumerate(hrs))
        sl = L["strip_legend"]
        strip = (f'<div class="strip-wrap"><div class="strip-inner">'
                 f'<div class="strip">{"".join(segs)}</div><div class="ticks">{ticks}</div></div></div>'
                 f'<div class="legend"><span><i class="sw a"></i>{sl[0]}</span><span><i class="sw b"></i>{sl[1]}</span>'
                 f'<span><i class="sw c"></i>{sl[2]}</span></div>')

    over = L["over_yes"] if d2['exceeds_sustained_threshold'] else L["over_no"]
    stage2 = f"""
<section id="s2" class="stage"><div class="st-h"><span class="st-n">2</span><div><h2>{L["s2_h"]}</h2>
<p class="st-q">{L["s2_q"]}</p></div></div>
<div class="grid4">{st("st_active", _f(d2['active_min']), both)}{st("st_longest", _f(d2['longest_block_min']), both)}
{st("st_away", d2['real_breaks'], both)}{st("st_longest_break", _f(d2['longest_break_min']), both)}</div>
<div class="card">{strip}</div>
<p class="ref">{L["s2_ref"].format(i=IDLE_MIN, h=SUSTAINED_CONTROL_HOURS, over=over)}</p>
</section>"""

    # ---- stage 3
    lbb = [b for b in d3["length_by_block"] if b["messages"]]
    lmax = max([b["median_len"] or 0 for b in lbb] or [1]) or 1
    lbars = "".join(f'<div class="lcol"><span class="ln">{_f(b["median_len"], 0)}</span>'
                    f'<div class="lbar" style="height:{(b["median_len"] or 0) / lmax * 100:.0f}%"></div>'
                    f'<span class="lx">{b["start"]}</span><span class="lx">{L["lx_msgs"].format(n=b["messages"])}</span></div>' for b in lbb)
    sw = d3["sharpest_window"]
    storm = (L["storm"].format(s=sw["start"], e=sw["end"], n=sw["switches"], r=sw["returns"]) if sw
             else L["storm_none"])
    stage3 = f"""
<section id="s3" class="stage"><div class="st-h"><span class="st-n">3</span><div><h2>{L["s3_h"]}</h2>
<p class="st-q">{L["s3_q"]}</p></div></div>
<div class="grid4">{st("st_switches", d3['switches'], both)}{st("st_returns", d3['returns'], both)}
{st("st_sw_density", _f(d3['per_active_hour']), both)}{st("st_concurrent", d3['peak_concurrent'], both, t=d3['peak_concurrent_at'] or '—')}</div>
<div class="two">
<div class="card"><h3>{L["storm_h"].format(w=WINDOW_MIN)} {both}</h3>
<p class="lead-s">{storm}</p>
<ul class="kv small"><li><span>{L["focus_run"]}</span><b>{L["focus_run_v"].format(n=d3['longest_focus_run'])}</b></li>
<li><span>{L["late_msgs"]}</span><b>{d3['late_night_messages']}</b></li></ul></div>
<div class="card"><h3>{L["len_h"]} {both}</h3><div class="lchart">{lbars}</div>
<p class="note">{L["len_note"].format(n=_f(d3['median_msg_len'], 0))}</p></div></div>
<p class="ref">{L["s3_ref"]}</p>
</section>"""

    # ---- stage 4
    vrows = "".join(f'<tr><td>{e(v["project"] or "")}</td><td class="num">{v["high_messages"]}</td>'
                    f'<td>{e(sep.join(tr("signals", s) for s in v["signals"]) or L["no_signal"])}</td></tr>' for v in d4["verification"])
    ep = f"{d4['explore_calls'] / d4['produce_calls']:.2f}" if d4["produce_calls"] else "—"
    vth = L["verify_th"]
    stage4 = f"""
<section id="s4" class="stage"><div class="st-h"><span class="st-n">4</span><div><h2>{L["s4_h"]}</h2>
<p class="st-q">{L["s4_q"]}</p></div></div>
<div class="grid4">{st("st_fast", _f(ratio * 100 if ratio is not None else None, 0), cc)}{st("st_long", d4['long_replies'], cc)}
{st("st_reply", _f(d4['median_reply_sec']), cc)}{st("st_retry", d4['retries_after_error'], cc, n=d4['tool_errors'])}</div>
<div class="two">
<div class="card"><h3>{L["verify_h"]} {cc}</h3>
{(f"<table><tr><th>{vth[0]}</th><th class='num'>{vth[1]}</th><th>{vth[2]}</th></tr>" + vrows + "</table>") if vrows else f'<p class="note">{L["verify_none"]}</p>'}</div>
<div class="card"><h3>{L["work_h"]}{" " if EN else ""}{cc}</h3><ul class="kv small">
<li><span>{L["explore"]}</span><b>{ep}</b></li><li><span>{L["agents"]}</span><b>{d4['agent_spawns']}</b></li></ul>
<p class="note">{L["work_note"]}</p></div></div>
<p class="ref">{L["s4_ref"].format(c=cfg['long_output_chars'], s=cfg['fast_accept_sec'])}</p>
</section>"""

    # ---- G10 block table
    trs = "".join(
        f'<tr><td>{r["start"]}–{r["end"]}</td><td>{e(tr("segments", r["segment"] or ""))}</td><td class="num">{_f(r["minutes"])}</td>'
        f'<td class="num">{_f(r["rest_before"])}</td><td class="num">{r["messages"]}</td>'
        f'<td class="num">{_f(r["switch_density"])}</td><td class="num">{_f(r["median_len"], 0)}</td>'
        f'<td class="num">{(str(r["fast_long"]) + " / " + str(r["long_replies"])) if r["long_replies"] else "—"}</td>'
        f'<td class="num">{r["high"]}</td><td>{e(sep.join(r["tools"]))}</td></tr>' for r in out["block_table"])
    tth = L["table_th"]
    table = f"""
<section class="stage"><h2 class="plain">{L["table_h"]}</h2>
<div class="card tbl"><table><tr><th>{tth[0]}</th><th>{tth[1]}</th><th class="num">{tth[2]}</th><th class="num">{tth[3]}</th>
<th class="num">{tth[4]}</th><th class="num">{tth[5]}</th><th class="num">{tth[6]}</th><th class="num">{tth[7]}</th><th class="num">{tth[8]}</th><th>{tth[9]}</th></tr>{trs}</table></div>
<p class="note">{L["table_note"].format(m=TABLE_MIN_BLOCK_MIN, n=TABLE_MIN_BLOCK_MSGS)}</p>
</section>"""

    # ---- details
    src = out["sources"]
    srows = "".join(f'<tr><td>{s["start"][11:16]}–{s["end"][11:16]}</td><td>{e(s["tool"])}</td><td>{e(s["project"])}</td>'
                    f'<td class="num">{s["human_messages"]}</td><td class="num">{_f(s["human_chars"])}</td>'
                    f'<td class="num">{_f(s.get("assistant_chars"))}</td><td class="num">{_f(s.get("tool_calls"))}</td></tr>'
                    for s in out["sessions"])
    sth = L["sessions_th"]
    method = "\n".join(f"<p>{p}</p>" for p in L["method"]).format(
        merged=src['claude_files_merged'], dups=_f(src['claude_fork_duplicates_removed']), bg=src['codex_background_threads'],
        i=IDLE_MIN, h=SUSTAINED_CONTROL_HOURS, f=cfg['fatigue_block_min'], c=cfg['long_output_chars'], s=cfg['fast_accept_sec'])
    details = f"""
<details><summary>{L["sessions_h"].format(n=len(out['sessions']))}</summary><div class="card tbl"><table>
<tr><th>{sth[0]}</th><th>{sth[1]}</th><th>{sth[2]}</th><th class="num">{sth[3]}</th><th class="num">{sth[4]}</th><th class="num">{sth[5]}</th><th class="num">{sth[6]}</th></tr>{srows}</table></div></details>
<details><summary>{L["method_h"]}</summary><div class="card method">
{method}
</div></details>"""

    css_zh = """
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
    # A CJK font draws curly quotes and apostrophes full-width ("You’ ve"), so
    # English pages put a Latin system font first and keep Noto Sans TC as fallback.
    latin = "-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,"
    css = css_zh.replace("font-family:'Noto Sans TC',", "font-family:" + latin + "'Noto Sans TC',") if EN else css_zh
    role_txt = (L["role"].format(r=ROLE_NAMES_EN.get(cfg['role'], cfg['role']) if EN else cfg['role'])
                if cfg['role'] else L["no_role"])
    mm, dd = int(out['date'][5:7]), int(out['date'][8:])
    h1 = L["h1_today"] if out["is_today"] else L["h1_past"].format(m=mm, d=dd)
    page = f"""<title>Decision Pulse {mm}/{dd}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=Noto+Sans+TC:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{css}</style>
<div class="page"{' lang="en"' if EN else ""}>
<header><div class="eyebrow">{L["eyebrow"].format(date=out['date'], wd=wd, gen=e(out['generated_at']), role=e(role_txt))}</div>
<h1>{h1}</h1></header>
<p class="lead">{e(summary_line(out))}</p>
{now_html}
{chain_html}
{sug_html}
{stage1}{stage2}{stage3}{stage4}{table}
<section>{details}</section>
<footer>{L["footer"]}</footer>
</div>
"""
    # Looking at a past day: every "today" on the page becomes "that day"
    # (suggestions, headings and notes share the same strings).
    if not out["is_today"]:
        for a, b in L["past_swaps"]:
            page = page.replace(a, b)
    return page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--role", default=None, help="try another role's preset without editing config (read at import)")
    ap.add_argument("--lang", default=None, help="interface language, zh-TW or en, without editing config (read at import)")
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
