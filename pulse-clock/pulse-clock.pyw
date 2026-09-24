"""Pulse Clock — a small desktop clock that shows decision-pulse's four stages.

The ring around the clock is the four stages of decision-pulse
(① density → ② recovery → ③ fatigue → ④ judgement), coloured by level.
Numbers come from decision-pulse's own scan_today.py --json; this program only
displays them, so the data rules stay in one place.

Left-click: show / hide the detail card.  Drag: move.  Right-click: menu.
No notifications, no sound, no blinking — you look when you want to.
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from datetime import datetime

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(APP_DIR, "state.json")
REFRESH_MIN = 30

# Where decision-pulse is (first match wins): this folder sits inside the skill,
# so its own scripts/ comes first; the rest cover a standalone copy.
SCRIPT_CANDIDATES = [
    os.path.join(os.path.dirname(APP_DIR), "scripts", "scan_today.py"),
    "~/.claude/skills/decision-pulse/scripts/scan_today.py",
    "~/.agents/skills/decision-pulse/scripts/scan_today.py",
    "~/.codex/skills/decision-pulse/scripts/scan_today.py",
]

STAGES = {  # key in decision-pulse's "levels", label, unit formatter
    "zh-TW": [
        ("density", "① 決策密度", lambda v: f"{v:.1f} 則／活躍小時"),
        ("recovery", "② 恢復窗口", lambda v: f"最長 {v:.0f} 分鐘沒休息"),
        ("fatigue", "③ 認知疲勞", lambda v: f"{v:.1f} 次切換／活躍小時"),
        ("judgement", "④ 判斷品質", lambda v: f"長內容秒回 {v * 100:.0f}%"),
    ],
    "en": [
        ("density", "① Decision density", lambda v: f"{v:.1f} messages / active hour"),
        ("recovery", "② Recovery window", lambda v: f"longest {v:.0f} min without a break"),
        ("fatigue", "③ Signs of fatigue", lambda v: f"{v:.1f} switches / active hour"),
        ("judgement", "④ Judgement quality", lambda v: f"quick replies to long outputs {v * 100:.0f}%"),
    ],
}

# The interface language comes from decision-pulse's own setting (config.local.json
# next to the skill, then the scan's JSON). Levels stay 低／中／高 in the data.
TXT = {
    "zh-TW": {
        "levels": {"低": "低", "中": "中", "高": "高"},
        "menu": ("看完整儀表板", "立即更新", "保持在最上層", "開機自動啟動", "結束"),
        "loading": "讀取中…", "streak": "已連續 {m} 分", "idle": "休息中 {m} 分", "not_started": "今天還沒開始",
        "not_found": "找不到 decision-pulse", "updating": "更新中…", "failed": "更新失敗 · 資料 {t}",
        "updated": "{t} 更新", "failed_short": "更新失敗", "no_json": "沒有 JSON 輸出",
        "card_head": "今天 · {t} 更新", "card_head_bare": "今天", "no_data": "資料不足",
        "last_break": "上次好好休息：{s}–{e}", "all_good": "今天的節奏還不錯，繼續保持。",
    },
    "en": {
        "levels": {"低": "low", "中": "medium", "高": "high"},
        "menu": ("Open full dashboard", "Refresh now", "Keep on top", "Start with Windows", "Quit"),
        "loading": "Loading…", "streak": "{m} min straight", "idle": "On a break · {m} min",
        "not_started": "Not started yet", "not_found": "decision-pulse not found", "updating": "Updating…",
        "failed": "Update failed · data {t}", "updated": "Updated {t}", "failed_short": "Update failed",
        "no_json": "No JSON output", "card_head": "Today · updated {t}", "card_head_bare": "Today",
        "no_data": "Not enough data", "last_break": "Last real break: {s}–{e}",
        "all_good": "Today's pace looks good. Keep it up.",
    },
}


def configured_lang():
    try:
        with open(os.path.join(os.path.dirname(APP_DIR), "config.local.json"), encoding="utf-8") as f:
            lang = json.load(f).get("lang")
    except (OSError, ValueError, AttributeError):
        lang = None
    return "en" if str(lang).lower().startswith("en") else "zh-TW"

C = {
    "bg": "#F4F6F3", "disc": "#FFFFFF", "text": "#1B211D", "muted": "#67716B",
    "border": "#DDE3DC", "track": "#E4E9E3",
    "低": "#3F9A5B", "中": "#D9A441", "高": "#D46A2E", "none": "#B8C0BA",
    "key": "#010203",  # window colour made transparent so the clock is round
}
FONT = "Microsoft JhengHei UI" if sys.platform == "win32" else "PingFang TC"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def find_script():
    state = load_state()
    paths = ([state["script"]] if state.get("script") else []) + SCRIPT_CANDIDATES
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isfile(p):
            return p
    return None


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(**kw):
    state = load_state()
    state.update(kw)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def python_cmd():
    """pythonw.exe runs .pyw; the scan itself needs python.exe (it prints)."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        alt = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.isfile(alt):
            return alt
    return exe


def run_scan(script, extra=()):
    out = subprocess.run(
        [python_cmd(), script, *extra], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, creationflags=NO_WINDOW,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-300:] or "scan failed")
    return out.stdout


def scan_json(script):
    raw = run_scan(script, ["--json"])
    start = raw.find("{")
    if start < 0:
        raise RuntimeError(TXT[configured_lang()]["no_json"])
    data, _ = json.JSONDecoder().raw_decode(raw[start:])
    return data


class PulseClock:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Pulse Clock")
        self.s = self.root.winfo_fpixels("1i") / 96  # DPI scale
        self.size = int(168 * self.s)
        self.data = None
        self.error = None
        self.updated_at = None
        self.card = None
        self.busy = False
        self.drag = None
        self.state = load_state()
        self.lang = configured_lang()
        self.topmost = tk.BooleanVar(value=self.state.get("topmost", True))

        r = self.root
        r.overrideredirect(True)
        r.attributes("-topmost", self.topmost.get())
        r.configure(bg=C["key"])
        if sys.platform == "win32":
            r.attributes("-transparentcolor", C["key"])
        x, y = self.state.get("pos", (r.winfo_screenwidth() - self.size - 40, 60))
        r.geometry(f"{self.size}x{self.size}+{x}+{y}")

        self.cv = tk.Canvas(r, width=self.size, height=self.size, bg=C["key"],
                            highlightthickness=0, bd=0)
        self.cv.pack()
        self.cv.bind("<ButtonPress-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        self.cv.bind("<Button-3>", self.on_menu)
        if sys.platform == "darwin":
            self.cv.bind("<Button-2>", self.on_menu)

        labels = self.t["menu"]
        self.menu = tk.Menu(r, tearoff=0, font=(FONT, 10))
        self.menu.add_command(label=labels[0], command=self.open_dashboard)
        self.menu.add_command(label=labels[1], command=self.refresh)
        self.menu.add_checkbutton(label=labels[2], variable=self.topmost,
                                  command=self.toggle_topmost)
        if sys.platform == "win32":
            self.autostart = tk.BooleanVar(value=os.path.isfile(startup_file()))
            self.menu.add_checkbutton(label=labels[3], variable=self.autostart,
                                      command=self.toggle_autostart)
        self.menu.add_separator()
        self.menu.add_command(label=labels[4], command=r.destroy)

        self.draw()
        self.tick()
        self.refresh()

    @property
    def t(self):
        return TXT[self.lang]

    # ---------- drawing ----------
    def level(self, key):
        if not self.data:
            return None
        return (self.data.get("levels", {}).get(key) or {}).get("level")

    def draw(self):
        cv, n, s = self.cv, self.size, self.s
        cv.delete("all")
        pad = 4 * s
        cv.create_oval(pad, pad, n - pad, n - pad, fill=C["disc"], outline=C["border"],
                       width=max(1, int(1 * s)))
        ring = 15 * s
        box = (ring, ring, n - ring, n - ring)
        w = 9 * s
        # tkinter angles: 0 = 3 o'clock, counter-clockwise; negative extent = clockwise.
        for i, (key, label, _) in enumerate(STAGES[self.lang]):
            start = 87 - 90 * i
            colour = C.get(self.level(key), C["none"])
            cv.create_arc(*box, start=start, extent=-84, style="arc", outline=colour,
                          width=w)
        # stage numbers just inside the ring, at each arc's midpoint
        rad = n / 2 - ring - 13 * s
        for i, mark in enumerate("①②③④"):
            ang = math.radians(45 - 90 * i)
            cx = n / 2 + rad * math.cos(ang)
            cy = n / 2 - rad * math.sin(ang)
            cv.create_text(cx, cy, text=mark, fill=C["muted"], font=(FONT, 8))

        now = datetime.now()
        cv.create_text(n / 2, n / 2 - 8 * s, text=now.strftime("%H:%M"), fill=C["text"],
                       font=(FONT, 22, "bold"))
        line, colour = self.status_line()
        cv.create_text(n / 2, n / 2 + 17 * s, text=line, fill=colour, font=(FONT, 9))
        foot = self.foot_line()
        cv.create_text(n / 2, n / 2 + 33 * s, text=foot, fill=C["muted"], font=(FONT, 7))

    def status_line(self):
        if self.error and not self.data:
            return self.error, C["高"]
        if not self.data:
            return self.t["loading"], C["muted"]
        now = self.data.get("now") or {}
        if now.get("active_now"):
            mins = now.get("current_block_min") or 0
            colour = C["高"] if now.get("over_fatigue") else C["text"]
            return self.t["streak"].format(m=f"{mins:.0f}"), colour
        idle = now.get("minutes_since_last_message")
        if isinstance(idle, (int, float)):
            return self.t["idle"].format(m=f"{idle:.0f}"), C["低"]
        return self.t["not_started"], C["muted"]

    def foot_line(self):
        if self.busy:
            return self.t["updating"]
        if self.error and self.data:
            return self.t["failed"].format(t=self.updated_at)
        if self.updated_at:
            return self.t["updated"].format(t=self.updated_at)
        return ""

    def tick(self):
        self.draw()
        now = datetime.now()
        self.root.after((60 - now.second) * 1000 + 50, self.tick)

    # ---------- data ----------
    def refresh(self):
        if self.busy:
            return
        script = find_script()
        if not script:
            self.error = self.t["not_found"]
            self.draw()
            self.root.after(REFRESH_MIN * 60 * 1000, self.refresh)
            return
        self.busy = True
        self.draw()

        def work():
            try:
                data, err = scan_json(script), None
            except Exception as exc:  # keep the last good data on failure
                data, err = None, str(exc)[:80] or self.t["failed_short"]
            self.root.after(0, lambda: self.on_data(data, err))

        threading.Thread(target=work, daemon=True).start()

    def on_data(self, data, err):
        self.busy = False
        if data:
            self.data, self.error = data, None
            lang = (data.get("config") or {}).get("lang")
            if lang in TXT:
                self.lang = lang
            self.updated_at = datetime.now().strftime("%H:%M")
        else:
            self.error = err
        self.draw()
        if self.card:
            self.show_card()
        self.root.after(REFRESH_MIN * 60 * 1000, self.refresh)

    # ---------- detail card ----------
    def toggle_card(self):
        if self.card:
            self.card.destroy()
            self.card = None
        else:
            self.show_card()

    def show_card(self):
        if self.card:
            self.card.destroy()
        s = self.s
        card = tk.Toplevel(self.root)
        card.overrideredirect(True)
        card.attributes("-topmost", self.topmost.get())
        card.configure(bg=C["border"])
        inner = tk.Frame(card, bg=C["disc"], padx=int(14 * s), pady=int(12 * s))
        inner.pack(padx=1, pady=1)
        head = self.t["card_head"].format(t=self.updated_at) if self.updated_at else self.t["card_head_bare"]
        tk.Label(inner, text=head, bg=C["disc"], fg=C["muted"], font=(FONT, 8)).pack(anchor="w")

        levels = (self.data or {}).get("levels", {})
        for key, label, fmt in STAGES[self.lang]:
            info = levels.get(key) or {}
            lv, val = info.get("level"), info.get("value")
            row = tk.Frame(inner, bg=C["disc"])
            row.pack(fill="x", pady=(int(5 * s), 0))
            tk.Label(row, text="●", bg=C["disc"], fg=C.get(lv, C["none"]),
                     font=(FONT, 10)).pack(side="left")
            tk.Label(row, text=label, bg=C["disc"], fg=C["text"],
                     font=(FONT, 10, "bold")).pack(side="left", padx=(4, 8))
            tk.Label(row, text=self.t["levels"].get(lv, lv) if lv else "—", bg=C["disc"], fg=C.get(lv, C["muted"]),
                     font=(FONT, 10, "bold")).pack(side="right")
            desc = fmt(val) if isinstance(val, (int, float)) else self.t["no_data"]
            tk.Label(inner, text=desc, bg=C["disc"], fg=C["muted"],
                     font=(FONT, 8)).pack(anchor="w", padx=(int(18 * s), 0))

        now = (self.data or {}).get("now") or {}
        brk = now.get("last_real_break")
        if brk:
            tk.Frame(inner, bg=C["border"], height=1).pack(fill="x", pady=int(8 * s))
            tk.Label(inner, text=self.t["last_break"].format(s=brk["start"], e=brk["end"]),
                     bg=C["disc"], fg=C["text"], font=(FONT, 9)).pack(anchor="w")

        sugs = (self.data or {}).get("suggestions") or []
        if sugs:
            top = sorted(sugs, key=lambda x: x.get("level") != "高")[0]
            tk.Frame(inner, bg=C["border"], height=1).pack(fill="x", pady=int(8 * s))
            # Tk wraps only at spaces; in Chinese keep "AI 寫了…" from leaving "AI"
            # alone on a line. English needs its spaces to wrap at all.
            text = top["text"] if self.lang == "en" else top["text"].replace(" ", "\u00a0")
            tk.Label(inner, text=text, bg=C["disc"], fg=C["text"], font=(FONT, 9),
                     wraplength=int(250 * s), justify="left").pack(anchor="w")
            tk.Label(inner, text=top.get("cite", ""), bg=C["disc"], fg=C["muted"],
                     font=(FONT, 7), wraplength=int(250 * s), justify="left").pack(anchor="w", pady=(3, 0))
        elif self.data:
            tk.Frame(inner, bg=C["border"], height=1).pack(fill="x", pady=int(8 * s))
            tk.Label(inner, text=self.t["all_good"], bg=C["disc"], fg=C["text"],
                     font=(FONT, 9)).pack(anchor="w")
        if self.error:
            tk.Label(inner, text=self.error, bg=C["disc"], fg=C["高"], font=(FONT, 8),
                     wraplength=int(250 * s), justify="left").pack(anchor="w", pady=(6, 0))

        card.bind("<Button-1>", lambda e: self.toggle_card())
        for w in card.winfo_children():
            w.bind("<Button-1>", lambda e: self.toggle_card())
        card.update_idletasks()
        cw, ch = card.winfo_width(), card.winfo_height()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        sw = self.root.winfo_screenwidth()
        cx = x - cw - 8 if x + self.size + cw + 8 > sw else x + self.size + 8
        card.geometry(f"+{max(0, cx)}+{max(0, y)}")
        self.card = card

    # ---------- interaction ----------
    def on_press(self, e):
        self.drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(), False)

    def on_drag(self, e):
        if not self.drag:
            return
        sx, sy, wx, wy, moved = self.drag
        dx, dy = e.x_root - sx, e.y_root - sy
        if moved or abs(dx) + abs(dy) > 4:
            self.drag = (sx, sy, wx, wy, True)
            self.root.geometry(f"+{wx + dx}+{wy + dy}")
            if self.card:
                self.card.destroy()
                self.card = None

    def on_release(self, e):
        if self.drag and self.drag[4]:
            save_state(pos=[self.root.winfo_x(), self.root.winfo_y()])
        else:
            self.toggle_card()
        self.drag = None

    def on_menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def toggle_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())
        save_state(topmost=self.topmost.get())

    def toggle_autostart(self):
        path = startup_file()
        try:
            if self.autostart.get():
                pyw = sys.executable if sys.executable.lower().endswith("pythonw.exe") \
                    else os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                with open(path, "w", encoding="utf-8") as f:
                    f.write('CreateObject("WScript.Shell").Run """%s"" ""%s""", 0\n'
                            % (pyw, os.path.abspath(__file__)))
            elif os.path.isfile(path):
                os.remove(path)
        except OSError:
            self.autostart.set(os.path.isfile(path))

    def open_dashboard(self):
        script = find_script()
        if not script:
            return
        out = os.path.join(tempfile.gettempdir(), "pulse-clock-dashboard.html")

        def work():
            try:
                run_scan(script, ["--html", out])
                webbrowser.open("file:///" + out.replace("\\", "/"))
            except Exception as exc:
                self.root.after(0, lambda: setattr(self, "error", str(exc)[:80]))

        threading.Thread(target=work, daemon=True).start()

    def run(self):
        self.root.mainloop()


def startup_file():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup", "pulse-clock.vbs")


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    PulseClock().run()
