#!/usr/bin/env python3
"""sesame agent — main.py — the entry point.

Interactive → the Textual TUI (tui.py). Piped/headless → the plain printer.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from config import Config
import log
import transcript as tx

HERE = Path(__file__).resolve().parent


def _install_defaults():
    p = HERE / "sesame.config.json"
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {}


class Printer:
    # loop.py offers the raw event stream to listeners that want the tool call ids
    # (the web UI pairs a result with its card). This one does not, but it must
    # answer the call.
    def on_raw(self, ev):
        pass

    """Headless listener: --print, pipes, CI."""

    def __init__(self, show_thinking=True):
        self.show_thinking = show_thinking
        self._mode = None

    def _w(self, s):
        sys.stdout.write(s)
        sys.stdout.flush()

    def on_thinking(self, delta):
        if self.show_thinking:
            if self._mode != "t":
                self._w("\n\033[2m✻ ")
                self._mode = "t"
            self._w(delta)

    def on_thinking_done(self):
        if self.show_thinking and self._mode == "t":
            self._w("\033[0m\n")
            self._mode = None

    def on_text(self, delta):
        if self._mode != "a":
            self._w("\n\033[38;5;114m●\033[0m ")
            self._mode = "a"
        self._w(delta)

    def on_text_done(self):
        if self._mode == "a":
            self._w("\n")
            self._mode = None

    def on_tool_call(self, name, args):
        primary = ""
        for k in ("command", "path", "query", "url", "task", "pattern", "selector"):
            if isinstance(args, dict) and args.get(k):
                primary = str(args[k]).replace("\n", " ")[:90]
                break
        self._w(f"\n\033[38;5;114m●\033[0m \033[1m{name}\033[0m\033[2m({primary})\033[0m\n")
        self._mode = None

    def on_tool_result(self, name, result):
        head = "\n".join(result.splitlines()[:6])
        self._w(f"\033[2m  ⎿ {head[:600]}\033[0m\n")

    def confirm(self, reason, name, args):
        self._w(f"\033[33m⚠ {name} needs approval ({reason}) — denied in headless mode; "
                f"pass --dangerously-skip-permissions to allow\033[0m\n")
        return False

    def on_status(self, state): ...
    def on_turn_done(self): ...

    def on_compaction(self, before):
        self._w(f"\033[2m  ⧉ compacting ({before:,} tokens)…\033[0m\n")

    def on_error(self, message):
        self._w(f"\n\033[31m✖ {message}\033[0m\n")

    def stop_requested(self):
        return False


class YesPrinter(Printer):
    def confirm(self, reason, name, args):
        self._w(f"\033[33m⚠ auto-approved: {name} ({reason})\033[0m\n")
        return True


def main():
    # subcommands before flags: `sesame doctor`, `sesame setup`
    if len(sys.argv) > 1 and sys.argv[1] in ("doctor", "setup"):
        import doctor as doc
        if sys.argv[1] == "setup":
            sys.exit(0 if doc.setup() else 1)
        sys.exit(doc.doctor(fix="--fix" in sys.argv))

    ap = argparse.ArgumentParser(prog="sesame", description="an agent in your terminal")
    ap.add_argument("--print", dest="prompt", metavar="TEXT", help="run one turn headless and exit")
    ap.add_argument("--dangerously-skip-permissions", action="store_true",
                    help="auto-approve every tool call (needed for --print to modify anything)")
    ap.add_argument("--no-thinking", action="store_true", help="hide reasoning in --print mode")
    ap.add_argument("--model", help="override model id")
    ap.add_argument("--provider", help="switch provider preset (see --providers)")
    ap.add_argument("--providers", action="store_true", help="list provider presets and exit")
    ap.add_argument("--effort", choices=["low", "medium", "high", "max"], help="reasoning depth")
    ap.add_argument("--resume", metavar="NAME", help="resume a saved session")
    ap.add_argument("--sessions", action="store_true", help="list saved sessions and exit")
    ap.add_argument("--check", action="store_true", help="verify config + connectivity and exit")
    args = ap.parse_args()

    if args.providers:
        import providers
        for n in providers.names():
            url, wire, model = providers.PRESETS[n]
            print(f"{n:<18} {wire:<10} {model:<42} {url}")
        return

    cfg = Config(_install_defaults())
    log.configure(cfg.log_file)
    if args.provider:
        cfg.switch_provider(args.provider)
    if args.model:
        cfg.switch_model(args.model)
    if args.effort:
        cfg.set_effort(args.effort)

    if args.sessions:
        import datetime as dt
        rows = tx.list_sessions()
        if not rows:
            print("(no saved sessions)")
        for r in rows:
            when = dt.datetime.fromtimestamp(r["updated"]).strftime("%Y-%m-%d %H:%M")
            print(f"{r['name']:<22} {when}  {r['turns']:>3} turns  "
                  f"{r['messages']:>4} msgs  ${r['cost']:.4f}")
        return

    if cfg.validate():  # no key yet — walk them through it instead of dying
        import doctor as doc
        if not doc.setup():
            sys.exit(1)
        cfg = Config(_install_defaults())

    if args.check:
        from shell import _retrying_stream
        try:
            msg = _retrying_stream(transcript=[{"role": "user", "content": "Reply with exactly OK."}],
                                   system="Reply with exactly OK.", tools=None, api=cfg.api,
                                   budget=cfg.budget, emit=lambda e: None)
            text = "".join(b.get("text", "") for b in msg["content"] if b["type"] == "text")
            print(f"ok · {cfg.model} · {cfg.api_type} wire · {cfg.base_url} · reply={text.strip()!r}")
        except Exception as exc:
            sys.exit(f"check failed: {exc}")
        return

    from loop import Listener, Loop

    # Headless: --print, or stdin is not a terminal (pipes, CI).
    if args.prompt or not sys.stdin.isatty():
        loop = Loop(cfg)
        loop.open(f"print-{time.strftime('%m%d-%H%M%S')}")
        ln = (YesPrinter if args.dangerously_skip_permissions else Printer)(
            show_thinking=not args.no_thinking)
        try:
            if args.prompt:
                loop.run(args.prompt, ln)
            else:
                for line in sys.stdin:
                    line = line.strip()
                    if not line or line in ("/quit", "/exit"):
                        break
                    print(f"\n\033[36m❯\033[0m {line}")
                    loop.run(line, ln)
        finally:
            loop.close()
        return

    from cli import App
    App(cfg, resume=args.resume).run()


if __name__ == "__main__":
    main()
