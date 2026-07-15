"""sesame agent, in a browser.

The same core as the terminal: Loop, shell.run, the same tools, the same safety
gate, the same session files. This adds an HTTP layer and nothing else. There is
no terminal being scraped here, and no second agent loop.

    ./run.sh web            http://127.0.0.1:9981
    ./run.sh web --port N

Events reach the browser over SSE. Everything else is JSON. The server binds to
localhost only: it runs your shell and edits your files, so it has no business on
a network interface.
"""

import json
import mimetypes
import os
import queue
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import checkpoint                                  # noqa: E402
import goals                                       # noqa: E402
import models                                      # noqa: E402
import project                                     # noqa: E402
import providers                                   # noqa: E402
import transcript as tx                            # noqa: E402
from config import Config                          # noqa: E402
from loop import Listener, Loop                    # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
LABEL = {"bash": "Shell", "read": "Read", "write": "Write", "edit": "Edit",
         "search": "Search", "list": "Files", "browse": "Fetch", "websearch": "Web search",
         "task": "Sub agent", "remember": "Remember", "forget": "Forget", "recall": "Recall",
         "browser_navigate": "Browser", "browser_read": "Browser read",
         "browser_click": "Browser click", "browser_type": "Browser type",
         "browser_screenshot": "Screenshot"}


class Hub:
    """Fan out events to every open browser tab, and keep a replay buffer so a
    reconnecting tab does not lose what it missed."""

    def __init__(self, keep=2000):
        self.lock = threading.Lock()
        self.log = []
        self.next_id = 1
        self.subs = []
        self.keep = keep

    def emit(self, ev):
        with self.lock:
            ev = dict(ev, id=self.next_id, at=time.time())
            self.next_id += 1
            self.log.append(ev)
            del self.log[:-self.keep]
            for q in list(self.subs):
                q.put(ev)
        return ev

    def subscribe(self, since=0):
        q = queue.Queue()
        with self.lock:
            missed = [e for e in self.log if e["id"] > since]
            self.subs.append(q)
        for e in missed:
            q.put(e)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)


class WebListener(Listener):
    def __init__(self, agent):
        self.a = agent
        self.think = ""
        self.think_at = 0.0
        self.answer = ""

    # the raw stream carries the ids, which is how a result finds its card
    def on_raw(self, ev):
        t = ev.get("type")
        if t == "thinking":
            if not self.think:
                self.think_at = time.monotonic()
            self.think += ev["text"]
            self.a.emit({"t": "reasoning_delta", "text": ev["text"]})
        elif t == "text":
            self.answer += ev["text"]
            self.a.emit({"t": "answer_delta", "text": ev["text"]})
        elif t == "tool_use":
            self._flush_reasoning()
            self._flush_answer()
            self.a.emit({"t": "tool", "call": ev["id"], "name": ev["name"],
                         "label": LABEL.get(ev["name"], ev["name"]),
                         "args": ev.get("input") or {}})
        elif t == "tool_result":
            self.a.emit({"t": "tool_result", "call": ev.get("id"), "name": ev["name"],
                         "content": ev.get("content", "")})

    def _flush_reasoning(self):
        if self.think.strip():
            secs = int(time.monotonic() - self.think_at)
            self.a.emit({"t": "reasoning_done", "text": self.think, "secs": secs,
                         "tokens": max(1, len(self.think) // 4)})
        self.think = ""

    def _flush_answer(self):
        if self.answer.strip():
            self.a.emit({"t": "answer_done", "text": self.answer})
        self.answer = ""

    def on_thinking_done(self):
        self._flush_reasoning()

    def on_text_done(self):
        self._flush_answer()

    def on_status(self, state):
        self.a.emit({"t": "status", "text": state})

    def on_steer(self, text):
        self.a.emit({"t": "steer_read"})

    def on_compaction(self, before):
        self.a.emit({"t": "notice", "text": f"context was full, compacted from {before:,} tokens"})

    def on_error(self, message):
        self.a.emit({"t": "error", "text": str(message)})

    def on_turn_done(self):
        self._flush_reasoning()
        self._flush_answer()

    def stop_requested(self):
        return self.a.stop

    def confirm(self, reason, name, args):
        """Runs on the turn's thread. Blocks it until the browser answers."""
        done = threading.Event()
        ask = {"id": f"ask-{int(time.time() * 1000)}", "name": name,
               "label": LABEL.get(name, name), "args": args, "reason": reason,
               "event": done, "answer": False}
        self.a.pending = ask
        self.a.emit({"t": "approval", "ask": ask["id"], "name": name,
                     "label": ask["label"], "args": args, "reason": reason})
        while not done.wait(0.25):
            if self.a.stop:                      # stopping cancels the question
                self.a.pending = None
                return False
        answer = ask["answer"]
        self.a.pending = None
        self.a.emit({"t": "approval_done", "ask": ask["id"], "answer": answer})
        return answer


def slots_url(base_url):
    """Where a local llama.cpp reports what it is chewing on, if it does.

    llama-server serves /slots next to /v1. Nothing else we speak to has an
    equivalent, and the endpoint is off in some builds, so this returns "" unless
    the server actually answers.
    """
    if not providers.is_local(base_url):
        return ""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    url = root.rstrip("/") + "/slots"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            data = json.loads(r.read())
        if isinstance(data, list) and data and "n_prompt_tokens_processed" in data[0]:
            return url
    except Exception:                                  # noqa: BLE001 no endpoint, no progress
        return ""
    return ""


class Agent:
    """One agent, shared by every tab. The browser is a view, not the owner."""

    def __init__(self):
        self.hub = Hub()
        self.cfg = Config()
        self.loop = Loop(self.cfg)
        self.ln = WebListener(self)
        self.busy = False
        self.stop = False
        self.pending = None
        self.session_name = None
        self.title = None
        self.slots = slots_url(self.cfg.base_url)
        self._compact_hinted = False
        self.new_session()
        threading.Thread(target=self._scheduler, daemon=True).start()

    def _scheduler(self):
        while True:
            time.sleep(2)
            if self.busy or self.pending or not self.loop.loop_job:
                continue
            if self.loop.loop_due(time.monotonic()):
                j = self.loop.loop_job
                j.fired(time.monotonic())
                self.loop._save()
                final = j.expired()              # 7 days on: last run, then delete
                self.emit({"t": "notice", "text": f"loop #{j.count}"
                          + (" (final — expired after 7 days)" if final else "")})
                prompt = j.prompt
                if final:
                    self.loop.loop_clear()
                self.send(prompt)

    def _watch_prefill(self, done):
        """A local model spends the first seconds reading your context, saying
        nothing. llama.cpp will tell us how far it has got, so show that instead
        of a spinner.

        Only the processed count and the rate are reported. While it works, the
        server's n_prompt_tokens tracks the batch in flight rather than the whole
        prompt, so a percentage here would be a number we made up.
        """
        last_n, last_t = 0, time.monotonic()
        while not done.wait(0.3):
            try:
                with urllib.request.urlopen(self.slots, timeout=1.5) as r:
                    slot = json.loads(r.read())[0]
            except Exception:                          # noqa: BLE001 the server went away
                return
            if not slot.get("is_processing"):
                continue
            n = int(slot.get("n_prompt_tokens_processed") or 0)
            if n <= last_n:
                continue
            now = time.monotonic()
            rate = (n - last_n) / max(0.001, now - last_t)
            last_n, last_t = n, now
            self.emit({"t": "prefill", "tokens": n, "rate": round(rate),
                       "cached": int(slot.get("n_prompt_tokens_cache") or 0)})

    def emit(self, ev):
        return self.hub.emit(ev)

    # ── session ──────────────────────────────────────────────────────────────
    def new_session(self):
        self.loop.reset()
        self.session_name = f"chat-{time.strftime('%m%d-%H%M%S')}"
        self.loop.open(self.session_name)
        self.title = None
        self.emit({"t": "session", "name": self.session_name, "history": []})

    def resume(self, name):
        if not self.loop.load(name):
            return False
        self.session_name = name
        self.title = None
        self.emit({"t": "session", "name": name, "history": self.history()})
        return True

    def history(self):
        """The saved conversation, in the same shapes the live stream uses, so the
        browser has one renderer and a resumed session looks like a live one."""
        out, names = [], {}
        for m in self.loop.messages:
            role, content = m.get("role"), m.get("content")
            if role == "user":
                if isinstance(content, str):
                    out.append({"t": "user", "text": content})
                    continue
                for b in content or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        body = b.get("content")
                        if isinstance(body, list):
                            body = "".join(x.get("text", "") for x in body if isinstance(x, dict))
                        out.append({"t": "tool_result", "call": b.get("tool_use_id"),
                                    "name": names.get(b.get("tool_use_id"), "tool"),
                                    "content": str(body or "")})
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        note = b["text"].split("]\n", 1)[-1].strip()
                        out.append({"t": "user", "text": note, "steer": True})
            elif role == "assistant" and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "thinking" and b.get("thinking", "").strip():
                        out.append({"t": "reasoning_done", "text": b["thinking"],
                                    "tokens": max(1, len(b["thinking"]) // 4)})
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        out.append({"t": "answer_done", "text": b["text"]})
                    elif b.get("type") == "tool_use":
                        names[b.get("id")] = b.get("name")
                        out.append({"t": "tool", "call": b.get("id"), "name": b.get("name"),
                                    "label": LABEL.get(b.get("name"), b.get("name")),
                                    "args": b.get("input") or {}})
        return out

    # ── a turn ───────────────────────────────────────────────────────────────
    def _slash(self, text):
        """/goal and /loop from the web input, so the browser has them too."""
        parts = text.split(None, 1)
        cmd, arg = parts[0], (parts[1].strip() if len(parts) > 1 else "")
        if cmd == "/goal":
            if arg == "pause": self.loop.goal_pause(); return "goal paused"
            if arg == "clear": self.loop.goal_clear(); return "goal cleared"
            if arg == "resume":
                if self.loop.goal_resume():
                    nxt = self.loop.goal_next()
                    if nxt: return ("__run__", nxt)
                return "no paused goal"
            if not arg:
                g = self.loop.goal
                return f"goal [{g.status}] turn {g.turns}: {g.objective}" if g else "no goal set"
            self.loop.set_goal(arg)
            return ("__run__", arg)
        if cmd == "/loop":
            if arg in ("stop", "clear", "off"): self.loop.loop_clear(); return "loop stopped"
            if not arg:
                j = self.loop.loop_job
                return f"loop every {j.interval}s: {j.prompt}" if j else "no loop"
            bits = arg.split(None, 1)
            secs = goals.parse_interval(bits[0])
            prompt = bits[1] if (secs and len(bits) > 1) else arg
            self.loop.set_loop(secs or goals.DEFAULT_LOOP_SECONDS, prompt)
            return ("__run__", prompt)
        return None

    def send(self, text):
        if text.startswith(("/goal", "/loop")):
            r = self._slash(text)
            if isinstance(r, tuple):              # a slash that kicks off a turn
                text = r[1]
            elif r is not None:
                self.emit({"t": "notice", "text": r})
                self.emit({"t": "config", **self.state()})
                return {"ok": True}
        if self.pending:                          # the answer to a permission question
            return {"ok": False, "error": "waiting for you to allow or deny the last action"}
        if self.busy:
            self.loop.steer(text)
            self.emit({"t": "user", "text": text, "steer": True})
            return {"ok": True, "queued": True}
        self.busy, self.stop = True, False
        if self.title is None:
            self.title = text.strip().splitlines()[0][:60]
        self.emit({"t": "user", "text": text})
        self.emit({"t": "title", "title": self.title, "session": self.session_name})
        self.emit({"t": "busy", "busy": True})
        threading.Thread(target=self._work, args=(text,), daemon=True).start()
        return {"ok": True, "queued": False}

    def _run_with_steers(self, text):
        self.loop.run(text, self.ln)
        leftover = self.loop.pending_steer()
        while leftover and not self.stop:
            self.emit({"t": "notice", "text": "it finished before reading your message, "
                                              "running it now"})
            self.loop.run(leftover, self.ln)
            leftover = self.loop.pending_steer()

    def _work(self, text):
        done = threading.Event()
        if self.slots:
            threading.Thread(target=self._watch_prefill, args=(done,), daemon=True).start()
        try:
            self._run_with_steers(text)
            # goal: keep pursuing until goal_done, budget, pause, or stop
            nxt = self.loop.goal_next()
            while nxt and not self.stop:
                self.emit({"t": "notice", "text": f"continuing toward the goal "
                                                  f"(turn {self.loop.goal.turns})"})
                self._run_with_steers(nxt)
                nxt = self.loop.goal_next()
            g = self.loop.goal
            if g and not self.stop and g.status == "complete":
                self.emit({"t": "notice", "text": f"goal complete: {g.summary}"})
            elif g and not self.stop and g.status == "budget_limited":
                self.emit({"t": "notice", "text": f"goal stopped after {g.turns} turns"})
        except Exception as exc:                  # noqa: BLE001 the UI must survive anything
            self.emit({"t": "error", "text": str(exc)})
        finally:
            done.set()
            if self.loop.goal and self.stop and self.loop.goal.status == "active":
                self.loop.goal_pause()
            self.busy = False
            self._compact_hint()
            self.emit({"t": "busy", "busy": False})
            self.emit({"t": "stats", **self.stats()})

    def answer(self, ask_id, verdict):
        p = self.pending
        if not p or p["id"] != ask_id:
            return False
        p["answer"] = "always" if verdict == "always" else (verdict == "yes")
        p["event"].set()
        return True

    # ── what the browser shows ───────────────────────────────────────────────
    def stats(self):
        st = self.loop.stats
        return {"tokens": st.context_tokens, "window": self.cfg.context_window,
                "cost": round(st.cost_usd, 4), "turns": st.turns,
                "input": st.input_tokens, "output": st.output_tokens}

    def state(self):
        c = self.cfg
        return {
            "model": c.model, "provider": c.active_provider, "baseUrl": c.base_url,
            "wire": c.api_type, "window": c.context_window, "effort": c.reasoning_effort,
            "thinkingTokens": c.thinking_budget, "maxTokens": c.max_output_tokens,
            "temperature": c.temperature, "confirmDanger": c.confirm_danger,
            "confirmAll": c.confirm_all, "toolBudget": c.tool_call_budget,
            "browserHeaded": c.browser_headed, "hasKey": bool(c.api_key),
            "keyHint": (c.api_key[:7] + "…") if c.api_key else "",
            "local": providers.is_local(c.base_url),
            "prefillProgress": bool(self.slots),
            "profiles": c.profiles(), "profile": c.profile,
            "providers": providers.names(), "workdir": c.workdir,
            "projectFiles": self.loop.project_files, "git": self.loop.git,
            "permissions": self.loop.perms, "tools": [t["name"] for t in self.loop.tools],
            "session": self.session_name, "title": self.title,
            "busy": self.busy, "stats": self.stats(),
            # turns() carries a Path per entry, which json will not take, and the
            # browser only wants the turn and what it would put back
            "undo": [{"turn": t["turn"], "files": t.get("files") or []}
                     for t in checkpoint.turns()[-6:]],
            "lastEvent": self.hub.next_id - 1,
        }


AGENT = Agent()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sesame"

    def log_message(self, *a):                    # quiet: the terminal is the user's
        pass

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    # ── routes ───────────────────────────────────────────────────────────────
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/events":
            return self._events(int((q.get("since") or ["0"])[0]))
        if u.path == "/api/state":
            return self._json(AGENT.state())
        if u.path == "/api/history":
            return self._json({"name": AGENT.session_name, "history": AGENT.history()})
        if u.path == "/api/sessions":
            return self._json({"sessions": self._sessions()})
        if u.path == "/api/models":
            return self._json(self._models(refresh=bool(q.get("refresh"))))
        return self._static(u.path)

    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()
        if u.path == "/api/send":
            text = (b.get("text") or "").strip()
            if not text:
                return self._json({"ok": False, "error": "empty"}, 400)
            return self._json(AGENT.send(text))
        if u.path == "/api/stop":
            AGENT.stop = True
            AGENT.emit({"t": "status", "text": "stopping"})
            return self._json({"ok": True})
        if u.path == "/api/approve":
            ok = AGENT.answer(b.get("ask"), b.get("answer"))
            return self._json({"ok": ok})
        if u.path == "/api/session/new":
            AGENT.new_session()
            return self._json({"ok": True, "state": AGENT.state()})
        if u.path == "/api/session/resume":
            ok = AGENT.resume(b.get("name") or "")
            return self._json({"ok": ok, "state": AGENT.state()})
        if u.path == "/api/session/delete":
            return self._json(self._delete(b.get("name") or ""))
        if u.path == "/api/undo":
            return self._json(self._undo(b))
        if u.path == "/api/compact":
            return self._json(self._compact())
        if u.path == "/api/config":
            return self._json(self._config(b))
        if u.path == "/api/profile":
            return self._json(self._profile(b))
        if u.path == "/api/permissions":
            return self._json(self._permissions(b))
        return self._json({"error": "not found"}, 404)

    # ── SSE ──────────────────────────────────────────────────────────────────
    def _events(self, since):
        # A browser reconnects on its own after any hiccup, and it reuses the URL
        # it was opened with. Resuming from the id in that URL replays everything
        # since the page loaded: the conversation appears twice, and an old
        # "busy: true" arrives after the real "busy: false" and freezes the UI.
        # Last-Event-ID is what the browser sends to say where it actually got to.
        resume = self.headers.get("Last-Event-ID")
        if resume and resume.isdigit():
            since = int(resume)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = AGENT.hub.subscribe(since)
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")   # or proxies hang up on us
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            AGENT.hub.unsubscribe(q)

    # ── actions ──────────────────────────────────────────────────────────────
    def _sessions(self):
        """list_sessions() carries a Path and no title. The browser needs neither a
        Path nor a filename: it needs the first thing you said."""
        out = []
        for r in sorted(tx.list_sessions(), key=lambda x: x["updated"], reverse=True):
            title = ""
            try:
                msgs = tx.parse(r["path"])[0]
                for m in msgs:
                    if m.get("role") == "user" and isinstance(m.get("content"), str):
                        title = m["content"].strip().splitlines()[0][:60]
                        break
            except OSError:
                pass
            out.append({"name": r["name"], "title": title, "updated": r["updated"],
                        "turns": r["turns"], "messages": r["messages"],
                        "cost": round(r["cost"], 4)})
        return out

    def _delete(self, name):
        if not name:
            return {"ok": False, "error": "no session"}
        path = tx._path(name)
        if not path.is_file():
            return {"ok": False, "error": "no such session"}
        path.unlink()
        if AGENT.session_name == name:          # you deleted the one you are in
            AGENT.new_session()
        AGENT.emit({"t": "config", **AGENT.state()})
        return {"ok": True}

    def _models(self, refresh=False):
        c = AGENT.cfg
        live = models.fetch(c.base_url, c.api_key, c.api_type, timeout=8 if refresh else 4)
        out = []
        for m in (live or models.known()):
            spec = models.spec(m)
            out.append({"id": m, "window": spec["window"], "in": spec["in"], "out": spec["out"],
                        "known": m in models.MODELS})
        return {"models": out, "source": c.active_provider if live else "built-in list"}

    def _undo(self, b):
        turns = checkpoint.turns()
        if not turns:
            return {"ok": False, "error": "nothing to undo"}
        turn = int(b.get("turn") or turns[-1]["turn"])
        notes = checkpoint.restore(turn)
        AGENT.emit({"t": "notice", "text": f"undo turn {turn}: " + ("; ".join(notes) or "no files")})
        return {"ok": True, "notes": notes}

    def _compact(self):
        before = AGENT.loop.stats.context_tokens
        did = AGENT.loop.compact_now(AGENT.ln)
        AGENT.emit({"t": "stats", **AGENT.stats()})
        return {"ok": bool(did), "before": before, "after": AGENT.loop.stats.context_tokens}

    def _config(self, b):
        c, action = AGENT.cfg, b.get("action")
        if action == "model":
            if not c.use_model(b.get("model") or ""):
                return {"ok": False, "needKey": True}
        elif action == "provider":
            if not c.switch_provider(b.get("provider") or "", b.get("apiKey") or ""):
                return {"ok": False, "needKey": True}
        elif action == "custom":
            url = (b.get("baseUrl") or "").strip()
            wire = b.get("wire") or ("anthropic" if "anthropic" in url else "openai")
            window = int(b.get("window") or 0)
            if not window:
                window = models.window_of(url, b.get("apiKey") or "", wire,
                                          b.get("model") or "", timeout=6)
            c.connect(url, b.get("model") or "local-model", b.get("apiKey") or "", wire,
                      window=window,
                      thinking="none" if providers.is_local(url) else None)
        elif action == "key":
            c.set("key", b.get("apiKey") or "")
            c.keys[c.active_provider] = b.get("apiKey") or ""
            project.save_config({"keys": c.keys})
        elif action == "effort":
            if c.set_effort(b.get("effort") or ""):
                project.save_config({"effort": c.reasoning_effort,
                                     "thinkingTokens": c.thinking_budget})
        elif action == "behavior":
            save = {}
            for key, attr, cast in (("confirmDanger", "confirm_danger", bool),
                                    ("confirmAll", "confirm_all", bool),
                                    ("toolCallBudget", "tool_call_budget", int),
                                    ("maxTokens", "max_output_tokens", int),
                                    ("browserHeaded", "browser_headed", bool)):
                if key in b:
                    setattr(c, attr, cast(b[key]))
                    save[key] = cast(b[key])
            if "temperature" in b:
                t = b["temperature"]
                c.temperature = None if t in ("", None) else float(t)
                save["temperature"] = c.temperature
            if save:
                project.save_config(save)
        elif action == "workdir":
            path = Path(b.get("path") or "").expanduser()
            if not path.is_dir():
                return {"ok": False, "error": "no such directory"}
            if AGENT.busy:
                return {"ok": False, "error": "it is working, stop it first"}
            os.chdir(path)
            AGENT.cfg = Config()
            AGENT.loop = Loop(AGENT.cfg)
            AGENT.ln = WebListener(AGENT)
            AGENT.new_session()
        else:
            return {"ok": False, "error": "unknown action"}
        AGENT.slots = slots_url(AGENT.cfg.base_url)
        AGENT.emit({"t": "config", **AGENT.state()})
        return {"ok": True, "state": AGENT.state()}

    def _profile(self, b):
        c, action, name = AGENT.cfg, b.get("action"), (b.get("name") or "").strip()
        if action == "save" and name:
            c.save_profile(name)
        elif action == "use" and name:
            if not c.use_profile(name):
                return {"ok": False, "error": "no such profile"}
        elif action == "delete" and name:
            c.delete_profile(name)
        else:
            return {"ok": False, "error": "unknown action"}
        AGENT.slots = slots_url(AGENT.cfg.base_url)
        AGENT.emit({"t": "config", **AGENT.state()})
        return {"ok": True, "state": AGENT.state()}

    def _permissions(self, b):
        perms = AGENT.loop.perms
        action = b.get("action")
        if action == "reset":
            perms["tools"], perms["prefixes"] = [], []
        elif action == "remove":
            rule, kind = b.get("rule"), b.get("kind")
            if kind in perms and rule in perms[kind]:
                perms[kind].remove(rule)
        project.save_permissions(perms)
        AGENT.emit({"t": "config", **AGENT.state()})
        return {"ok": True, "permissions": perms}

    # ── static ───────────────────────────────────────────────────────────────
    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (STATIC / rel).resolve()
        if not str(f).startswith(str(STATIC)) or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype)


def serve(port=9981, open_browser=True):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    where = AGENT.cfg.profile or ("local" if providers.is_local(AGENT.cfg.base_url)
                                  else AGENT.cfg.active_provider)
    print(f"sesame agent on {url}", flush=True)
    print(f"  model    {AGENT.cfg.model} · {where}", flush=True)
    print(f"  folder   {AGENT.cfg.workdir}", flush=True)
    print("  ctrl-c to stop", flush=True)
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.4), __import__("webbrowser").open(url)),
                         daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        AGENT.loop.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(prog="sesame web")
    ap.add_argument("--port", type=int, default=9981)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    serve(a.port, not a.no_open)
