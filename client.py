"""群聊客户端：一个窗口里看三方的原话，也直接在这里说话。

  pythonw client.py [--cwd <project dir>]

Shows every line of the room's channel with its speaker. What you type goes into the
channel as your words, addressed by the two toggles (GPT / Claude) unless the text
itself names someone (@gpt / @claude / @all):
  GPT     -> queued as a follow-up on the room's Codex window; GPT answers there, in its own harness
  Claude  -> wakes the Claude session waiting in the room (its wait.py exits)
A local window with no network endpoint: only what you type here can start a turn.
Model / effort / permission settings are shown, not changed: each harness keeps its own controls.
"""
import argparse, ctypes, glob, os, re, sys, threading, time, webbrowser
import tkinter as tk
from tkinter import ttk
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

COLORS = {"user": "#2f6fdb", "claude": "#c15f3c", "gpt": "#0f8f6f"}
NAMES = {"user": "你", "claude": "Claude", "gpt": "GPT"}
WHERE = {"codex": "在 Codex", "claude-code": "在 Claude Code", "client": "在群聊"}
CLAUDE_MODES = {"default": "每次询问", "acceptEdits": "自动接受编辑", "plan": "计划模式", "auto": "自动模式",
                "bypassPermissions": "跳过权限", "dontAsk": "不询问"}
FONT = "Microsoft YaHei UI"
MONO = "Consolas"
INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\([^)\s]+\))")
PREFS = os.path.join(C.BRIDGE_DIR, "client-prefs.json")


def active_rooms():
    rooms = [C.room_by_dir(os.path.dirname(f)) for f in glob.glob(os.path.join(C.ROOMS_DIR, "*", "room.json"))]
    def recency(r):
        p = C.channel_path(r)
        return os.path.getmtime(p) if os.path.exists(p) else 0
    return sorted(rooms, key=recency, reverse=True)


def gpt_access(cfg):
    sandbox, approval = str(cfg.get("sandbox") or ""), str(cfg.get("approval") or "")
    if '"disabled"' in sandbox or "danger" in sandbox:
        access = "完全访问"
    elif "read" in sandbox.lower():
        access = "只读"
    elif sandbox:
        access = "工作区可写"
    else:
        access = ""
    if approval and approval != "never":
        access += f"（审批：{approval}）"
    return access


class Client:
    def __init__(self, root, start_room):
        self.root = root
        self.rooms = active_rooms()
        self.room = start_room or (self.rooms[0] if self.rooms else None)
        self.last = 0
        self.links = 0
        self.notice = ("", 0)
        self.gpt_cfg = {}
        prefs = C.load_json(PREFS, {})
        self.to_gpt = tk.BooleanVar(value=prefs.get("gpt", True))
        self.to_claude = tk.BooleanVar(value=prefs.get("claude", False))
        root.title("群聊")
        root.geometry("940x880")
        root.minsize(600, 440)
        root.configure(bg="#ffffff")

        top = tk.Frame(root, bg="#ffffff")
        top.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(top, text="房间", font=(FONT, 10), bg="#ffffff", fg="#666").pack(side="left")
        self.pick = ttk.Combobox(top, state="readonly", width=24, values=[r["name"] for r in self.rooms])
        self.pick.pack(side="left", padx=(6, 0))
        self.pick.bind("<<ComboboxSelected>>", self.switch_room)
        self.status = tk.Label(top, text="", font=(FONT, 10), bg="#ffffff", fg="#555", anchor="e")
        self.status.pack(side="right", fill="x", expand=True)
        self.settings = tk.Label(root, text="", font=(FONT, 9), bg="#ffffff", fg="#8a8a8a", anchor="e")
        self.settings.pack(fill="x", padx=14, pady=(2, 6))

        mid = tk.Frame(root, bg="#ffffff")
        mid.pack(fill="both", expand=True, padx=14)
        self.log = tk.Text(mid, wrap="word", font=(FONT, 11), bg="#ffffff", fg="#1f1f1f", relief="flat",
                           padx=6, pady=6, spacing1=2, spacing3=2, cursor="arrow", state="disabled")
        sb = ttk.Scrollbar(mid, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.setup_tags()

        bottom = tk.Frame(root, bg="#f4f4f2")
        bottom.pack(fill="x", side="bottom")
        bar = tk.Frame(bottom, bg="#f4f4f2")
        bar.pack(fill="x", padx=14, pady=(8, 2))
        tk.Label(bar, text="发给", font=(FONT, 10), bg="#f4f4f2", fg="#555").pack(side="left", padx=(0, 6))
        for name, var, who in (("GPT", self.to_gpt, "gpt"), ("Claude", self.to_claude, "claude")):
            tk.Checkbutton(bar, text=name, variable=var, indicatoron=False, font=(FONT, 10, "bold"), width=8,
                           relief="flat", bd=0, fg=COLORS[who], selectcolor="#dfe9f7" if who == "gpt" else "#f7e3da",
                           bg="#e9e9e5", activebackground="#dcdcd6", command=self.save_prefs).pack(side="left", padx=(0, 6), ipady=2)
        tk.Label(bar, text="Enter 发送 · Shift+Enter 换行 · 消息里写了 @ 时按 @ 为准",
                 font=(FONT, 9), bg="#f4f4f2", fg="#777").pack(side="right")
        row = tk.Frame(bottom, bg="#f4f4f2")
        row.pack(fill="x", padx=14, pady=(4, 12))
        self.inp = tk.Text(row, height=4, wrap="word", font=(FONT, 11), relief="flat", padx=8, pady=6,
                           bg="#ffffff", highlightthickness=1, highlightbackground="#d8d8d2", highlightcolor="#9a9a92")
        self.inp.pack(side="left", fill="x", expand=True)
        tk.Button(row, text="发送", font=(FONT, 10), relief="flat", bg="#1f1f1f", fg="#ffffff",
                  activebackground="#3a3a3a", activeforeground="#ffffff", padx=16, command=self.send).pack(side="left", padx=(8, 0), fill="y")
        self.inp.bind("<Return>", self.on_return)
        self.inp.focus_set()

        if self.room:
            self.pick.set(self.room["name"])
            root.title(f"群聊 · {self.room['name']}")
        else:
            self.write_system("还没有任何房间。请先在 Claude Code 里让 Claude 为项目开一个共享窗口。")
        self.poll()
        self.refresh_status()
        self.refresh_gpt_cfg()

    # ---------- rendering ----------
    def setup_tags(self):
        t = self.log
        for who, color in COLORS.items():
            t.tag_configure(f"head-{who}", foreground=color, font=(FONT, 10, "bold"), spacing1=14)
        t.tag_configure("meta", foreground="#9a9a9a", font=(FONT, 9))
        t.tag_configure("body", lmargin1=14, lmargin2=14, rmargin=8)
        t.tag_configure("bold", font=(FONT, 11, "bold"))
        t.tag_configure("h", font=(FONT, 12, "bold"), spacing1=6)
        t.tag_configure("code", font=(MONO, 10), background="#f1f1ee")
        t.tag_configure("codeblock", font=(MONO, 10), background="#f4f4f1", lmargin1=24, lmargin2=24)
        t.tag_configure("li", lmargin1=22, lmargin2=36)
        t.tag_configure("link", foreground="#2f6fdb", underline=True)
        t.tag_configure("system", foreground="#888888", font=(FONT, 9), justify="center", spacing1=10)

    def at_bottom(self):
        return self.log.yview()[1] > 0.995

    def write_system(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", ("system",))
        self.log.configure(state="disabled")
        self.log.see("end")

    def inline(self, line, tags):
        pos = 0
        for m in INLINE.finditer(line):
            if m.start() > pos:
                self.log.insert("end", line[pos:m.start()], tags)
            tok = m.group(0)
            if tok.startswith("**"):
                self.log.insert("end", tok[2:-2], tags + ("bold",))
            elif tok.startswith("`"):
                self.log.insert("end", tok[1:-1], tags + ("code",))
            else:
                label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", tok).groups()
                self.links += 1
                tag = f"link{self.links}"
                self.log.tag_bind(tag, "<Button-1>", lambda e, u=url: webbrowser.open(u) if u.startswith("http") else None)
                self.log.tag_bind(tag, "<Enter>", lambda e: self.log.configure(cursor="hand2"))
                self.log.tag_bind(tag, "<Leave>", lambda e: self.log.configure(cursor="arrow"))
                self.log.insert("end", label, tags + ("link", tag))
            pos = m.end()
        self.log.insert("end", line[pos:] + "\n", tags)

    def markdown(self, text, tags):
        in_code = False
        for raw in text.split("\n"):
            if raw.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code or re.match(r"\s*\|.*\|\s*$", raw):
                self.log.insert("end", raw + "\n", tags + ("codeblock",))
                continue
            m = re.match(r"#{1,6}\s+(.*)", raw)
            if m:
                self.inline(m.group(1), tags + ("h",))
                continue
            m = re.match(r"(\s*)([-*]|\d+\.)\s+(.*)", raw)
            if m:
                bullet = "• " if m.group(2) in ("-", "*") else m.group(2) + " "
                self.log.insert("end", "    " * (len(m.group(1)) // 2) + bullet, tags + ("li",))
                self.inline(m.group(3), tags + ("li",))
                continue
            self.inline(raw, tags)

    def show(self, e):
        who = e["from"]
        stick = self.at_bottom()
        self.log.configure(state="normal")
        self.log.insert("end", NAMES.get(who, who), (f"head-{who}",))
        meta = "  " + (e.get("ts") or "")[11:16]
        if who == "user" and WHERE.get(e.get("via")):
            meta += " · " + WHERE[e["via"]]
        if e.get("to"):
            meta += " → " + "、".join(NAMES.get(t, t) for t in e["to"])
        self.log.insert("end", meta + "\n", ("meta",))
        self.markdown(e["text"].rstrip(), ("body",))
        self.log.configure(state="disabled")
        if stick:
            self.log.see("end")

    # ---------- channel and status ----------
    def switch_room(self, _=None):
        name = self.pick.get()
        self.room = next(r for r in self.rooms if r["name"] == name)
        self.root.title(f"群聊 · {name}")
        self.last = 0
        self.gpt_cfg = {}
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def poll(self):
        try:
            if self.room:
                es = C.read_after(self.room, self.last)
                for e in es:
                    self.show(e)
                if es:
                    self.last = es[-1]["seq"]
        except Exception as ex:
            C.log_error("client poll", ex)
        self.root.after(600, self.poll)

    def refresh_gpt_cfg(self):
        def work(room):
            tids = C.codex_threads_of(room)
            if tids:
                self.gpt_cfg = C.codex_thread_settings(tids[0])
        if self.room:
            threading.Thread(target=work, args=(self.room,), daemon=True).start()
        self.root.after(20000, self.refresh_gpt_cfg)

    def refresh_status(self):
        try:
            if self.room:
                self.status.configure(text=self.status_text())
                self.settings.configure(text=self.settings_text())
        except Exception as ex:
            C.log_error("client status", ex)
        self.root.after(1000, self.refresh_status)

    def status_text(self):
        st, now = C.get_status(self.room), time.time()
        g = st.get("gpt", {})
        tids = C.codex_threads_of(self.room)
        queued = [q for q in C.pending_queue(self.room) if q["thread"] in tids]
        if not tids:
            gpt = "GPT：还没有 Codex 窗口"
        elif g.get("state") == "busy" and now - g.get("at", 0) < 1800:
            gpt = f"GPT：回答中 {int(now - g['at'])}s"
        elif queued and now - min(q["at"] for q in queued) > 10:
            gpt = "GPT：消息停在 Codex 队列里，请到 Codex 窗口点一下发送"
        elif queued:
            gpt = "GPT：已交给 Codex，等待开始…"
        else:
            gpt = "GPT：空闲"
        c = st.get("claude", {})
        if c.get("state") == "busy" and now - c.get("at", 0) < 1800:
            claude = f"Claude：回答中 {int(now - c['at'])}s"
        elif C.claude_listening(self.room):
            claude = "Claude：在线"
        else:
            claude = "Claude：未待命（下次被调用时看到）"
        notice = self.notice[0] if now - self.notice[1] < 10 else ""
        return "    ".join(x for x in (notice, gpt, claude) if x)

    def settings_text(self):
        st = C.get_status(self.room)
        g = dict(st.get("gpt_cfg", {}))
        g.update({k: v for k, v in self.gpt_cfg.items() if v})
        c = st.get("claude_cfg", {})
        gpt = " · ".join(x for x in (g.get("model"), g.get("effort") and f"思考 {g['effort']}", gpt_access(g)) if x)
        claude = " · ".join(x for x in (c.get("model"), c.get("effort") and f"思考 {c['effort']}",
                                         CLAUDE_MODES.get(c.get("mode"), c.get("mode"))) if x)
        return f"GPT：{gpt or '—'}      Claude：{claude or '—'}      （在各自的窗口里调整）"

    # ---------- sending ----------
    def save_prefs(self):
        C.save_json(PREFS, {"gpt": self.to_gpt.get(), "claude": self.to_claude.get()})

    def on_return(self, event):
        if event.state & 0x1:  # Shift+Enter: newline
            return None
        self.send()
        return "break"

    def send(self):
        text = self.inp.get("1.0", "end").strip()
        if not text or not self.room:
            return
        named = [w for w in ("gpt", "claude") if C.MENTION[w].search(text)] or \
                (["gpt", "claude"] if C.MENTION["all"].search(text) else [])
        to = named or [w for w, v in (("gpt", self.to_gpt), ("claude", self.to_claude)) if v.get()]
        self.inp.delete("1.0", "end")
        e = C.append(self.room, "user", text, "client", session="client", to=to)
        if "gpt" in to:
            threading.Thread(target=self.queue_gpt, args=(e,), daemon=True).start()
        if "claude" in to and not C.claude_listening(self.room):
            self.notice = ("Claude 现在没在等待，这句话它下次被调用时会看到", time.time())
        if not to:
            self.notice = ("没选任何人：这句话只记进对话，谁都不叫醒", time.time())

    def queue_gpt(self, e):
        try:
            sent = C.queue_to_gpt(self.room, e)
            bad = [s for s in sent if s[1] != 0]
            if not sent:
                self.notice = ("这个房间还没有 Codex 窗口", time.time())
            elif bad:
                self.notice = ("交给 Codex 失败：" + bad[0][2][:60], time.time())
        except Exception as ex:
            C.log_error("client queue", ex)
            self.notice = (f"交给 Codex 失败：{ex}", time.time())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd")
    a = ap.parse_args()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    room = None
    if a.cwd:
        r = C.resolve_room(a.cwd)
        room = r if C.room_active(r) else None
    root = tk.Tk()
    Client(root, room)
    root.mainloop()


if __name__ == "__main__":
    main()
