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

STAGES = [  # key in decision-pulse's "levels", label, unit formatter
    ("density", "① 決策密度", lambda v: f"{v:.1f} 則／活躍小時"),
    ("recovery", "② 恢復窗口", lambda v: f"最長 {v:.0f} 分鐘沒休息"),
    ("fatigue", "③ 認知疲勞", lambda v: f"{v:.1f} 次切換／活躍小時"),
    ("judgement", "④ 判斷品質", lambda v: f"長內容秒回 {v * 100:.0f}%"),
]

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
        raise RuntimeError("沒有 JSON 輸出")
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

        self.menu = tk.Menu(r, tearoff=0, font=(FONT, 10))
        self.menu.add_command(label="看完整儀表板", command=self.open_dashboard)
        self.menu.add_command(label="立即更新", command=self.refresh)
        self.menu.add_checkbutton(label="保持在最上層", variable=self.topmost,
                                  command=self.toggle_topmost)
        if sys.platform == "win32":
            self.autostart = tk.BooleanVar(value=os.path.isfile(startup_file()))
            self.menu.add_checkbutton(label="開機自動啟動", variable=self.autostart,
                                      command=self.toggle_autostart)
        self.menu.add_separator()
        self.menu.add_command(label="結束", command=r.destroy)

        self.draw()
        self.tick()
        self.refresh()

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
        for i, (key, label, _) in enumerate(STAGES):
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
            return "讀取中…", C["muted"]
        now = self.data.get("now") or {}
        if now.get("active_now"):
            mins = now.get("current_block_min") or 0
            colour = C["高"] if now.get("over_fatigue") else C["text"]
            return f"已連續 {mins:.0f} 分", colour
        idle = now.get("minutes_since_last_message")
        if isinstance(idle, (int, float)):
            return f"休息中 {idle:.0f} 分", C["低"]
        return "今天還沒開始", C["muted"]

    def foot_line(self):
        if self.busy:
            return "更新中…"
        if self.error and self.data:
            return f"更新失敗 · 資料 {self.updated_at}"
        if self.updated_at:
            return f"{self.updated_at} 更新"
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
            self.error = "找不到 decision-pulse"
            self.draw()
            self.root.after(REFRESH_MIN * 60 * 1000, self.refresh)
            return
        self.busy = True
        self.draw()

        def work():
            try:
                data, err = scan_json(script), None
            except Exception as exc:  # keep the last good data on failure
                data, err = None, str(exc)[:80] or "更新失敗"
            self.root.after(0, lambda: self.on_data(data, err))

        threading.Thread(target=work, daemon=True).start()

    def on_data(self, data, err):
        self.busy = False
        if data:
            self.data, self.error = data, None
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
        head = f"今天 · {self.updated_at} 更新" if self.updated_at else "今天"
        tk.Label(inner, text=head, bg=C["disc"], fg=C["muted"], font=(FONT, 8)).pack(anchor="w")

        levels = (self.data or {}).get("levels", {})
        for key, label, fmt in STAGES:
            info = levels.get(key) or {}
            lv, val = info.get("level"), info.get("value")
            row = tk.Frame(inner, bg=C["disc"])
            row.pack(fill="x", pady=(int(5 * s), 0))
            tk.Label(row, text="●", bg=C["disc"], fg=C.get(lv, C["none"]),
                     font=(FONT, 10)).pack(side="left")
            tk.Label(row, text=label, bg=C["disc"], fg=C["text"],
                     font=(FONT, 10, "bold")).pack(side="left", padx=(4, 8))
            tk.Label(row, text=lv or "—", bg=C["disc"], fg=C.get(lv, C["muted"]),
                     font=(FONT, 10, "bold")).pack(side="right")
            desc = fmt(val) if isinstance(val, (int, float)) else "資料不足"
            tk.Label(inner, text=desc, bg=C["disc"], fg=C["muted"],
                     font=(FONT, 8)).pack(anchor="w", padx=(int(18 * s), 0))

        now = (self.data or {}).get("now") or {}
        brk = now.get("last_real_break")
        if brk:
            tk.Frame(inner, bg=C["border"], height=1).pack(fill="x", pady=int(8 * s))
            tk.Label(inner, text=f"上次好好休息：{brk['start']}–{brk['end']}",
                     bg=C["disc"], fg=C["text"], font=(FONT, 9)).pack(anchor="w")

        sugs = (self.data or {}).get("suggestions") or []
        if sugs:
            top = sorted(sugs, key=lambda x: x.get("level") != "高")[0]
            tk.Frame(inner, bg=C["border"], height=1).pack(fill="x", pady=int(8 * s))
            # Tk wraps only at spaces; keep "AI 寫了…" from leaving "AI" alone on a line.
            tk.Label(inner, text=top["text"].replace(" ", " "), bg=C["disc"], fg=C["text"], font=(FONT, 9),
                     wraplength=int(250 * s), justify="left").pack(anchor="w")
            tk.Label(inner, text=top.get("cite", ""), bg=C["disc"], fg=C["muted"],
                     font=(FONT, 7), wraplength=int(250 * s), justify="left").pack(anchor="w", pady=(3, 0))
        elif self.data:
            tk.Frame(inner, bg=C["border"], height=1).pack(fill="x", pady=int(8 * s))
            tk.Label(inner, text="今天的節奏還不錯，繼續保持。", bg=C["disc"], fg=C["text"],
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
