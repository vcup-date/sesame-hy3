"""sesame agent — loop.py — the Loop/Listener interface the TUI expects.

This exists so the original sesame TUI (tui.py, Textual) runs unchanged. It is
an adapter, not a second agent loop: run() delegates to shell.run(), and the
Listener callbacks are fed from the shell's event stream.

Loop owns the session state the TUI asks about (messages, stats, memory) and
the concerns that live outside the loop (permissions, checkpoints, context).
"""

import os
import queue
import sys
import threading
from dataclasses import dataclass, field
from datetime import date

import checkpoint
import compact as compaction
import context as ctx
import danger
import goals
import log
import models
import project
import transcript as tx
from history import validate_and_repair
from memory import Memory, make_memory_tools
from shell import run as shell_run, _retrying_stream, APIError
from subagent import make_subagent_tool
import tools as toolsmod
from tools import TOOLS
import browser


class Listener:
    """Exactly the protocol the TUI implements."""
    def on_raw(self, ev): ...        # the shell's own event, ids and all
    def on_thinking(self, delta): ...
    def on_thinking_done(self): ...
    def on_text(self, delta): ...
    def on_text_done(self): ...
    def on_tool_call(self, name, args): ...
    def on_tool_result(self, name, result): ...
    def confirm(self, reason, name, args): return True
    def on_status(self, state): ...
    def on_turn_done(self): ...
    def on_compaction(self, before): ...
    def on_steer(self, text): ...
    def on_error(self, message): ...
    def stop_requested(self): return False


@dataclass
class Stats:
    context_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0


class Stop(Exception):
    """Raised inside the event stream when the user asks the run to stop."""


class Loop:
    def __init__(self, cfg):
        self.cfg = cfg
        self.memory = Memory()
        self.messages = []
        self.stats = Stats()
        self.session = None
        self.perms = project.load_permissions()
        self.project_doc, self.project_files = project.instructions()
        self.git = project.git_status()
        self._ln = None
        self._lock = threading.Lock()
        self._steer = queue.Queue()  # messages you typed while it was working
        self._session_name = None
        self.goal = None             # a durable objective the agent keeps pursuing
        self.loop_job = None         # a prompt re-run on an interval
        browser.STATE["headed"] = cfg.browser_headed
        log.configure(cfg.log_file)
        self.tools = (TOOLS + browser.TOOLS + make_memory_tools(self.memory)
                      + self._goal_tools()
                      + [make_subagent_tool(tools=TOOLS, api=self.cfg.api,
                                            budget=self.cfg.budget,
                                            on_event=self._sub_event)])
        toolsmod.LIMITS["bash_timeout"] = cfg.bash_timeout
        toolsmod.LIMITS["tool_output"] = cfg.tool_output_limit
        toolsmod.PROGRESS["emit"] = self._progress

    # ── prompt ───────────────────────────────────────────────────────────────
    def system(self):
        base = ("You are sesame, a fast agent that reasons continuously and reaches for tools "
                "from inside your own thinking. As you think through a task, call tools the "
                "moment you need a fact or an action, read the result, and keep reasoning in the "
                "same flow until the work is done. Prefer surgical edit over rewriting whole "
                "files. Delegate open-ended exploration to the task tool. Launch servers and long "
                "jobs in the background and report the PID. Keep final answers short and concrete. "
                "Destructive actions require the user to approve them when asked.")
        env = f"Working directory: {self.cfg.workdir}\nToday: {date.today().isoformat()}"
        if self.git:
            env += f"\n{self.git}"
        parts = [base, env]
        if self.project_doc:
            parts.append("Project instructions (authoritative — follow them):\n" + self.project_doc)
        mem = self.memory.prompt_block()
        if mem:
            parts.append(mem)
        return "\n\n".join(parts)

    # ── steering ─────────────────────────────────────────────────────────────
    def steer(self, text):
        """Queue a message to be injected into the RUNNING turn at its next step."""
        self._steer.put(text)

    def _pop_steer(self):
        """Drain everything typed since the last step into one note. The shell
        calls this at each tool-result boundary."""
        notes = []
        while True:
            try:
                notes.append(self._steer.get_nowait())
            except queue.Empty:
                break
        if not notes:
            return None
        joined = "\n".join(notes)
        return ("[the user interjected while you were working — read this and adjust "
                f"course now, do not start over]\n{joined}")

    def pending_steer(self):
        """Anything the model never got to see (it finished first)."""
        notes = []
        while True:
            try:
                notes.append(self._steer.get_nowait())
            except queue.Empty:
                break
        return "\n".join(notes) if notes else None

    # ── shell event stream → Listener callbacks ──────────────────────────────
    def _progress(self, ev):
        if self._ln:
            self._ln.on_status(f"running… {ev['elapsed']}s")

    def _sub_event(self, ev):
        if self._ln and ev.get("type") == "tool_use":
            self._ln.on_status(f"sub-agent: {ev['name']}")

    def _event(self, ev):
        ln = self._ln
        if ln is None:
            return
        raw = getattr(ln, "on_raw", None)   # optional: the terminal never needed the
        if raw:                             # call ids, the web UI does, to pair a
            raw(ev)                         # result with its card
        if ln.stop_requested():
            # KeyboardInterrupt, not a custom exception: it is the ONE abort path
            # shell.run already knows how to unwind cleanly — it lands the partial
            # thinking/text/tool_use in the transcript instead of dropping it. A
            # custom exception flew straight past that handler and lost the work
            # the agent had already done before you interrupted it.
            raise KeyboardInterrupt()
        t = ev["type"]
        if t == "block_start":
            if ev["block_type"] == "thinking":
                ln.on_status("thinking")
            elif ev["block_type"] == "text":
                ln.on_status("writing")
        elif t == "thinking":
            ln.on_thinking(ev["text"])
        elif t == "text":
            ln.on_text(ev["text"])
        elif t == "tool_use":
            self._flush(ln)
            ln.on_tool_call(ev["name"], ev["input"])
            ln.on_status(f"running {ev['name']}")
        elif t == "tool_result":
            ln.on_tool_result(ev["name"], ev["content"])
        elif t == "retry":
            why = ev.get("why") or "failed"
            ln.on_status(f"{why} — retrying in {ev['wait']}s ({ev['attempt']}/{ev['of']})")
        elif t == "steer":
            ln.on_steer(ev["text"])
        elif t == "context":
            ln.on_compaction(ev.get("before", 0))
        elif t in ("truncated", "budget_stop"):
            ln.on_error("output limit hit mid-call — the call was not run"
                        if t == "truncated" else
                        f"stopped: tool-call budget ({self.cfg.budget['tool_calls']}) exhausted")
        elif t == "done":
            self._flush(ln)

    def _flush(self, ln):
        ln.on_thinking_done()
        ln.on_text_done()

    # ── safety gate ──────────────────────────────────────────────────────────
    def _safety(self, call):
        """Only DANGEROUS actions prompt.

        Prompting because a tool writes — mkdir, a new file, an ordinary edit —
        is noise, and noise is what teaches you to press "y" without reading.
        danger.check() decides; everything it clears just runs, and /undo can
        roll back any file that was touched. cfg.confirm_all restores the old
        ask-on-every-write behaviour for anyone who wants it.
        """
        name, args = call["name"], call.get("input") or {}
        checkpoint.snapshot(self.stats.turns, name, args)  # before anything runs
        tool = next((t for t in self.tools if t["name"] == name), None)
        writes = not (tool and tool.get("read_only"))
        reason = danger.check(name, args) if self.cfg.confirm_danger else None

        if reason is None:
            # ordinary work: allowed unless you asked to confirm every write
            if not (self.cfg.confirm_all and writes):
                return {"allow": True}
            if project.allowed(self.perms, name, args):
                return {"allow": True}
        else:
            # dangerous: a tool-level "always allow" does NOT cover this. Only an
            # explicit rule for this exact command does (bash:rm -rf build).
            if project.prefix_allowed(self.perms, name, args):
                return {"allow": True}

        if self._ln is None:
            return {"allow": True}

        ok = self._ln.confirm(reason or f"run {name}?", name, args)
        if ok == "always":
            if reason:
                # remember the command, not the tool: "always allow bash" must
                # never become a blanket pass for rm -rf
                rule = project.remember_prefix(self.perms, name, args)
                if rule:
                    self.perms["prefixes"].append(rule)
            else:
                self.perms["tools"].append(name)
            project.save_permissions(self.perms)
            return {"allow": True}
        return {"allow": True} if ok else {"allow": False, "reason": "denied by user"}

    # ── the turn ─────────────────────────────────────────────────────────────
    def run(self, text, ln):
        self._ln = ln
        self.messages.append({"role": "user", "content": text})
        self.messages[:] = validate_and_repair(self.messages)
        self.stats.turns += 1
        self._journal({"role": "user", "content": text})
        checkpoint.prune()
        log.write("turn", f"#{self.stats.turns} {text[:80]!r}")
        try:
            res = shell_run(transcript=self.messages, system=self.system(), tools=self.tools,
                            budget=self.cfg.budget, safety=self._safety, on_event=self._event,
                            journal=self._journal, api=self.cfg.api,
                            transform_context=self._context(), steer=self._pop_steer)
            self._account(res["spent"])
            if res.get("aborted"):
                self.messages[:] = validate_and_repair(self.messages)
                ln.on_status("stopped")
            else:
                ln.on_turn_done()
        except (Stop, KeyboardInterrupt):
            # the partial trajectory is already in self.messages (shell.run put it
            # there); repair only fills in results for calls that never ran
            self._flush(ln)
            self.messages[:] = validate_and_repair(self.messages)
            ln.on_status("stopped")
        except APIError as exc:
            self._flush(ln)
            if exc.context_overflow:
                ln.on_compaction(self.stats.context_tokens)
                self.compact_now(ln)
                ln.on_error("context was full — compacted; send your message again")
            else:
                log.write("error", str(exc))
                ln.on_error(str(exc))
            ln.on_status("error")
        except Exception as exc:
            self._flush(ln)
            log.write("error", f"loop: {exc}")
            ln.on_error(str(exc))
            ln.on_status("error")
        finally:
            self._ln = None
            self._save()

    def _context(self):
        return ctx.make_manager(window=self.cfg.context_window, system_fn=self.system,
                                summarize_fn=self._summarize,
                                on_event=lambda ev: self._event(ev))

    def _summarize(self, messages, should_stop=None):
        def complete(system, msgs):
            def emit(ev):                 # the summary stream emits text deltas; ticking
                if should_stop and should_stop():   # the stop check here lets esc cancel it
                    raise KeyboardInterrupt()
            msg = _retrying_stream(transcript=msgs, system=system, tools=None, api=self.cfg.api,
                                   budget={"thinking_tokens": 1024, "effort": "low"},
                                   emit=emit)
            return "".join(b.get("text", "") for b in msg["content"] if b["type"] == "text")
        return compaction.compact(complete, messages, keep=self.cfg.compact_keep_recent)

    def compact_now(self, ln, should_stop=None):
        before = self.stats.context_tokens
        new, did = self._summarize(self.messages, should_stop=should_stop)
        if did:
            self.messages[:] = validate_and_repair(new)
            self.stats.context_tokens = ctx.estimate_tokens(self.messages, self.system())
            ln.on_compaction(before)
            return True
        return bool(ctx._elide(self.messages, keep_recent=4))

    def _account(self, spent):
        for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"):
            setattr(self.stats, k, getattr(self.stats, k) + spent.get(k, 0))
        self.stats.cost_usd += models.cost(self.cfg.model, spent)
        self.stats.context_tokens = ctx.estimate_tokens(self.messages, self.system())

    # ── goal: a durable objective the agent keeps pursuing across turns ──────
    def set_goal(self, objective, budget=None):
        self.goal = goals.Goal(objective, budget=budget, base_out=self.stats.output_tokens)
        self._save()
        return self.goal

    def goal_next(self):
        """After a turn: the next continuation prompt, or None if the goal is done,
        out of budget, paused, or cleared. Updates the goal's status."""
        if not self.goal:
            return None
        p = self.goal.next_prompt(self.stats.output_tokens)
        self._save()
        return p

    def goal_complete(self, summary=""):
        if self.goal:
            self.goal.status = "complete"
            self.goal.summary = summary
            self._save()

    def goal_pause(self):
        if self.goal and self.goal.status == "active":
            self.goal.status = "paused"
            self._save()

    def goal_resume(self):
        if self.goal and self.goal.status in ("paused", "budget_limited"):
            self.goal.status = "active"
            self._save()
            return True
        return False

    def goal_clear(self):
        had = self.goal is not None
        self.goal = None
        self._save()
        return had

    # ── loop: a prompt re-run on an interval ─────────────────────────────────
    def set_loop(self, interval, prompt):
        self.loop_job = goals.LoopJob(interval, prompt)
        self._save()
        return self.loop_job

    def loop_clear(self):
        had = self.loop_job is not None
        self.loop_job = None
        self._save()
        return had

    def loop_due(self, now):
        return bool(self.loop_job) and self.loop_job.due(now)

    # ── the tools the model can call to drive these itself ───────────────────
    def _goal_tools(self):
        def _set_goal(inp):
            obj = str(inp.get("objective") or "").strip()
            if not obj:
                return {"ok": False, "content": "objective is required"}
            budget = inp.get("budget_tokens")
            self.set_goal(obj, int(budget) if budget else None)
            return {"ok": True, "content": f"Goal set. I will keep working toward it, one "
                                           f"step per turn, until it is done: {obj}"}

        def _goal_done(inp):
            if not self.goal or self.goal.status not in ("active", "paused"):
                return {"ok": False, "content": "there is no active goal to complete"}
            self.goal_complete(str(inp.get("summary") or "done"))
            return {"ok": True, "content": "Goal marked complete."}

        def _set_loop(inp):
            prompt = str(inp.get("prompt") or "").strip()
            if not prompt:
                return {"ok": False, "content": "prompt is required"}
            secs = goals.parse_interval(str(inp.get("every") or "")) or goals.DEFAULT_LOOP_SECONDS
            self.set_loop(secs, prompt)
            return {"ok": True, "content": f"Loop set: every {secs}s I will run: {prompt}. "
                                           f"Call stop_loop to cancel it."}

        def _stop_loop(inp):
            if self.loop_clear():
                return {"ok": True, "content": "The loop is stopped."}
            return {"ok": True, "content": "There was no loop running."}

        return [
            {"name": "set_goal", "read_only": False,
             "description": ("Set a durable objective to keep working toward across many turns. "
                             "After each turn you will be asked to continue until you call "
                             "goal_done. Use for a big task with a clear finish (a migration, a "
                             "feature, a game). Optional budget_tokens caps the output spend."),
             "input_schema": {"type": "object",
                              "properties": {"objective": {"type": "string"},
                                             "budget_tokens": {"type": "integer"}},
                              "required": ["objective"]},
             "execute": _set_goal},
            {"name": "goal_done", "read_only": False,
             "description": ("Declare the current goal complete (or report that you are blocked). "
                             "Only call this when the objective is actually achieved, or you truly "
                             "cannot proceed. It stops the goal loop."),
             "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}},
                              "required": ["summary"]},
             "execute": _goal_done},
            {"name": "set_loop", "read_only": False,
             "description": ("Re-run a prompt on an interval, e.g. every '10m'. Use for polling or "
                             "a periodic check. It repeats until stopped. To STOP a loop, call "
                             "stop_loop — do NOT call set_loop with a prompt like 'stop'."),
             "input_schema": {"type": "object",
                              "properties": {"prompt": {"type": "string"},
                                             "every": {"type": "string",
                                                       "description": "e.g. 30s, 5m, 1h"}},
                              "required": ["prompt"]},
             "execute": _set_loop},
            {"name": "stop_loop", "read_only": False,
             "description": "Stop the running loop. Call this when the user asks to stop or cancel it.",
             "input_schema": {"type": "object", "properties": {}},
             "execute": _stop_loop},
        ]

    # ── session (markdown, append-only) ──────────────────────────────────────
    def open(self, name):
        """Name the session but do NOT create the file yet — launching sesame and
        typing nothing should not litter .sesame/sessions with empty files."""
        self.session = None
        self._session_name = name
        return None

    def _journal(self, msg):
        if self.session is None:          # first real message → now create the file
            self.session = tx.Session(self._session_name, self.cfg.model)
        try:
            self.session.append(msg)
        except OSError:
            pass

    def _save(self):
        if self.session:
            try:
                data = dict(vars(self.stats))
                if self.goal:
                    data["goal"] = self.goal.to_dict()
                if self.loop_job:
                    data["loop"] = self.loop_job.to_dict()
                self.session.stats(data)
            except OSError:
                pass

    def save_as(self, name):
        if self.session:
            return self.session.rename(name)
        self.session = tx.Session(name, self.cfg.model)   # nothing said yet: still make it
        self._session_name = name
        return self.session.path

    def load(self, name):
        data = tx.load(name)
        if not data or not data["messages"]:
            return False
        self._session_name = name
        self.messages[:] = validate_and_repair(data["messages"])
        st = data["stats"]
        self.stats = Stats(**{k: st.get(k, 0) for k in vars(Stats()) if k in st})
        self.goal = goals.Goal.from_dict(st["goal"]) if st.get("goal") else None
        self.loop_job = goals.LoopJob.from_dict(st["loop"]) if st.get("loop") else None
        if self.loop_job and self.loop_job.expired():   # a loop older than 7 days is not restored
            self.loop_job = None
        self.session = tx.Session(data["name"], self.cfg.model)
        return True

    def reset(self):
        self.messages.clear()
        self.stats = Stats()
        self.goal = None
        self.loop_job = None
        self.memory.clear_session()
        if self.session:
            try:
                self.session.end()
            except OSError:
                pass
        self.session = None

    def close(self):
        if self.session:
            try:
                self.session.stats(vars(self.stats))
                self.session.end()
            except OSError:
                pass
        browser.shutdown()
