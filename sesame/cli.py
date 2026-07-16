"""sesame agent — cli.py — the interactive terminal UI.

This is a normal terminal program, not a full-screen app. Output is printed as
ordinary lines, so your terminal keeps it in its scrollback: you scroll with the
wheel, select with the mouse, and copy with ⌘C exactly like in any other program.
There is no scrollbar because there is no scroll region.

Only the input prompt is live, at the bottom. While the agent works, typing
steers it (the message is handed to the model at its next step) and esc stops it.
"""

import asyncio
import os
import re
import shutil
import signal
import textwrap
import select as _select
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.shortcuts import prompt as pt_prompt
from prompt_toolkit.application import Application
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML, FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

import checkpoint
import goals
import models
import providers
import team
import transcript as tx
from loop import Listener, Loop

# ── colours ──────────────────────────────────────────────────────────────────
TTY = sys.stdout.isatty()


def _c(code, s):
    return f"\x1b[{code}m{s}\x1b[0m" if TTY else s


def dim(s): return _c("2", s)
def bold(s): return _c("1", s)
def green(s): return _c("38;5;114", s)
def cyan(s): return _c("36", s)
def yellow(s): return _c("33", s)
def red(s): return _c("31", s)
def magenta(s): return _c("35", s)


# Each team member gets a stable colour from its name, so John is the same hue
# every time he speaks — in the roster, in a review, in a report.
_MEMBER_COLORS = ["38;5;39", "38;5;208", "38;5;141", "38;5;78", "38;5;213",
                  "38;5;220", "38;5;44", "38;5;168", "38;5;114", "38;5;111"]


def member_color(name):
    code = _MEMBER_COLORS[sum(ord(c) for c in name) % len(_MEMBER_COLORS)]
    return lambda s: _c(code, s)


def out(s=""):
    sys.stdout.write(s + "\n")
    sys.stdout.flush()


def cols():
    # shutil, not os: os.get_terminal_size() reports 0 columns when the terminal
    # has no size set (a pty opened without a winsize, some CI runners), and a
    # width of 0 silently shrinks every rule, preview and table to nothing.
    return shutil.get_terminal_size((80, 24)).columns or 80


def rows():
    return shutil.get_terminal_size((80, 24)).lines or 24


term_rows = rows          # select() takes a `rows` argument of its own


def tok(n):
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M".replace(".0M", "M")
    if n >= 100_000:                     # 262.1k reads as noise; 262k is the fact
        return f"{round(n / 1000)}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


# ── markdown ─────────────────────────────────────────────────────────────────
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_ITAL = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_STRIKE = re.compile(r"~~([^~]+)~~")


def inline(text):
    """Bold, italic, inline code, links, strikethrough.

    Links go FIRST: once other rules have inserted escape codes, the text is full
    of "[1m" sequences and a link pattern will happily match across them.
    """
    if not TTY:
        return text
    text = _LINK.sub(lambda m: _c("4", m.group(1)) + dim(f" ({m.group(2)})"), text)
    text = _CODE.sub(lambda m: cyan(m.group(1)), text)
    text = _BOLD.sub(lambda m: bold(m.group(1) or m.group(2)), text)
    text = _ITAL.sub(lambda m: _c("3", m.group(1)), text)
    text = _STRIKE.sub(lambda m: _c("9", m.group(1)), text)
    return text


def _plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def unmark(text):
    """Markdown source to readable prose, for one line previews (the thinking
    line showed things like '- **Previous Close:** $407.76')."""
    text = _LINK.sub(lambda m: m.group(1), text)
    text = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"~~([^~]+)~~", lambda m: m.group(1), text)
    text = re.sub(r"`([^`]+)`", lambda m: m.group(1), text)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", lambda m: m.group(1), text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text)
    text = re.sub(r"^\s*\d+[.)]\s+", "", text)
    text = re.sub(r"^\s*>\s*", "", text)
    text = re.sub(r"^\s*\|\s*|\s*\|\s*$", "", text)
    return re.sub(r"\s+", " ", text).strip()


class Markdown:
    """Renders the answer as it streams.

    Lines are emitted as soon as they are complete, except for tables and fenced
    code, which are held until the block closes so they can be laid out.
    """

    def __init__(self, emit):
        self.emit = emit          # emit(text, raw=False) prints one line
        self.table = []
        self.code = []
        self.in_code = False
        self.lang = ""

    def feed(self, line):
        stripped = line.strip()

        if self.in_code:
            if stripped.startswith("```"):
                self._flush_code()
            else:
                self.code.append(line)
            return

        if stripped.startswith("```"):
            self._flush_table()
            self.in_code = True
            self.lang = stripped[3:].strip()
            self.code = []
            return

        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            self.table.append(stripped)
            return
        self._flush_table()

        if not stripped:
            self.emit("")
        elif stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped[level:].strip()
            self.emit(bold(_c("38;5;208", text)) if level <= 2 else bold(text))
        elif stripped.startswith(">"):
            self.emit(dim("│ ") + inline(stripped[1:].strip()))
        elif re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            self.emit(dim("─" * min(40, cols() - 6)))
        elif re.match(r"^[-*+]\s+", stripped):
            indent = " " * (len(line) - len(line.lstrip()))
            self.emit(f"{indent}{cyan('•')} " + inline(re.sub(r"^[-*+]\s+", "", stripped)))
        elif re.match(r"^\d+[.)]\s+", stripped):
            num, rest = re.match(r"^(\d+)[.)]\s+(.*)$", stripped).groups()
            indent = " " * (len(line) - len(line.lstrip()))
            self.emit(f"{indent}{cyan(num + '.')} " + inline(rest))
        else:
            self.emit(inline(line))

    def close(self):
        self._flush_table()
        self._flush_code()

    # ── code ─────────────────────────────────────────────────────────────────
    def _flush_code(self):
        if not self.in_code:
            return
        self.in_code = False
        width = min(cols() - 8, 100)
        if self.lang:
            self.emit(dim(f"┌─ {self.lang} " + "─" * max(0, width - len(self.lang) - 4)))
        else:
            self.emit(dim("┌" + "─" * width))
        for ln in self.code:
            self.emit(dim("│ ") + cyan(ln[:width]))
        self.emit(dim("└" + "─" * width))
        self.code = []

    # ── tables ───────────────────────────────────────────────────────────────
    def _flush_table(self):
        rows = self.table
        self.table = []
        if not rows:
            return
        cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
        # a |---|---| row marks the line above it as the header
        sep = next((i for i, row in enumerate(cells)
                    if row and all(re.fullmatch(r":?-{2,}:?", c or "-") for c in row)), None)
        if sep is not None and sep > 0:
            header = cells[sep - 1]
            body = cells[sep + 1:]
        else:
            header, body = cells[0], cells[1:]

        n = max(len(header), max((len(r) for r in body), default=0))
        header += [""] * (n - len(header))
        body = [r + [""] * (n - len(r)) for r in body]
        widths = [max([len(_plain(inline(h)))] + [len(_plain(inline(r[i]))) for r in body])
                  for i, h in enumerate(header)]
        widths = [min(w, max(8, (cols() - 6) // n)) for w in widths]

        def row_line(cells_, style):
            out_cells = []
            for i, c in enumerate(cells_):
                rendered = style(inline(c))
                # pad on the VISIBLE width: styled text carries escape codes that
                # take no columns, and padding on the raw string breaks alignment
                pad = " " * max(0, widths[i] - len(_plain(rendered)))
                out_cells.append(rendered + pad)
            return dim("│ ") + dim(" │ ").join(out_cells) + dim(" │")

        top = dim("┌─" + "─┬─".join("─" * w for w in widths) + "─┐")
        mid = dim("├─" + "─┼─".join("─" * w for w in widths) + "─┤")
        bot = dim("└─" + "─┴─".join("─" * w for w in widths) + "─┘")
        self.emit(top)
        self.emit(row_line(header, bold))
        self.emit(mid)
        for r in body:
            self.emit(row_line(r, lambda x: x))
        self.emit(bot)


LABEL = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit", "list": "List",
         "search": "Search", "websearch": "WebSearch", "browse": "Fetch", "task": "Task",
         "browser_navigate": "Browser", "browser_read": "Browser", "browser_click": "Click",
         "browser_type": "Type", "browser_screenshot": "Screenshot",
         "remember": "Remember", "forget": "Forget", "recall": "Recall",
         "set_goal": "Goal", "goal_done": "GoalDone", "set_loop": "Loop", "stop_loop": "StopLoop",
         "hire": "Hire", "fire": "Fire", "watch": "Watch", "delegate": "Delegate",
         "team_status": "Team"}
PRIMARY = ["command", "path", "query", "url", "task", "pattern", "selector", "content",
           "match", "old_string", "role", "objective", "name", "prompt"]


def arg_of(name, args):
    for k in PRIMARY:
        if isinstance(args, dict) and args.get(k):
            return str(args[k]).replace("\n", " ")[:90]
    return ""


def cursor_row():
    """Ask the terminal which row the cursor is on. None if it will not say."""
    if not (sys.stdin.isatty() and TTY):
        return None
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return None
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[6n")
        sys.stdout.flush()
        buf = b""
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            if not _select.select([fd], [], [], 0.05)[0]:
                continue
            buf += os.read(fd, 64)
            if b"R" in buf:
                break
        m = re.search(rb"\x1b\[(\d+);(\d+)R", buf)
        return int(m.group(1)) if m else None
    except OSError:
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def park_bottom():
    """Push the cursor to the bottom of the screen.

    prompt_toolkit pins its toolbar to the last rows of the terminal but draws
    the prompt wherever the cursor is, so any free space above the toolbar turns
    into blank rows under ❯. A dialog that erases itself frees exactly that much
    space, which is why the gap grew with the size of the dialog.
    """
    if not TTY:
        return
    lines = rows()
    row = cursor_row()
    if row is None:
        return
    need = (lines - 3) - row          # rule + input + rule + status
    if need > 0:
        sys.stdout.write("\n" * need)
        sys.stdout.flush()


def run_dialog(app):
    """Run a dialog. It erases itself when it closes, so the screen it filled is
    free again: park the cursor back at the bottom or the prompt floats up."""
    animate(app)
    result = app.run()
    park_bottom()
    return result


# ── animation ────────────────────────────────────────────────────────────────
BASE = "#6c6c6c"
# a comet: bright head, fading tail
COMET = ["#ffffff", "#eeeeee", "#c6c6c6", "#9e9e9e", "#808080", "#767676"]
PULSE = ["#00afaf", "#5fd7d7", "#87ffff", "#afffff", "#87ffff", "#5fd7d7"]
# cool to warm, used across the effort track: faster is cool, smarter is warm
RAMP = ["#00afaf", "#00d7af", "#5fd787", "#afd75f", "#ffd75f", "#ffaf5f",
        "#ff875f", "#ff5faf", "#d75fff", "#af5fff"]


def _mix(hex_a, hex_b, t):
    a = [int(hex_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def shimmer_frags(text, i, base=BASE, band=COMET, speed=2, gap=24):
    """A comet sweeping left to right: bright head, tail fading into the base."""
    n = max(len(text), 1)
    head = (i * speed) % (n + gap)
    frags = []
    for k, ch in enumerate(text):
        d = head - k
        colour = band[d] if 0 <= d < len(band) else base
        frags.append((f"fg:{colour}", ch))
    return frags


def wave_frags(text, i, ramp=RAMP, speed=1):
    """The whole word lit by a colour wave rolling through it."""
    return [(f"fg:{ramp[(k * 2 + i * speed) % len(ramp)]} bold", ch)
            for k, ch in enumerate(text)]


def gradient_track(width, i, spark=True):
    """A cool-to-warm track with a bright spark running along it."""
    frags = []
    pos = (i * 2) % (width + 20)
    for k in range(width):
        t = k / max(width - 1, 1)
        idx = t * (len(RAMP) - 1)
        lo, hi = int(idx), min(int(idx) + 1, len(RAMP) - 1)
        colour = _mix(RAMP[lo], RAMP[hi], idx - lo)
        if spark and 0 <= pos - k < 3:
            colour = _mix(colour, "#ffffff", 1 - (pos - k) / 3)
        frags.append((f"fg:{colour}", "─"))
    return frags


LEVEL_STYLE = [
    ("#8a8a8a", "flat"),      # low     grey, still
    ("#5fd787", "glow"),      # medium  one colour, breathing gently
    ("#ffaf5f", "comet"),     # high    warm, with a comet running through it
    (None, "wave"),           # max     the full spectrum, rolling
]


def level_frags(text, level, i):
    """Style a level name by how hard that level thinks."""
    colour, kind = LEVEL_STYLE[min(level, len(LEVEL_STYLE) - 1)]
    if kind == "flat":
        return [(f"fg:{colour}", text)]
    if kind == "glow":
        return [(f"fg:{_mix(colour, '#ffffff', abs(((i // 2) % 10) - 5) / 5)} bold", text)]
    if kind == "comet":
        return shimmer_frags(text, i, base=colour,
                             band=["#ffffff", "#ffe7c6", "#ffd7af"], speed=1, gap=6)
    return wave_frags(text, i)


def pulse(i):
    return PULSE[(i // 2) % len(PULSE)]


def breathe(i, colour="#00afaf"):
    """A glow that swells and fades, for the marker."""
    t = abs(((i // 2) % 12) - 6) / 6
    return _mix(colour, "#ffffff", t)


def animate(app):
    """Redraw a dialog ~10x a second so its shimmer moves."""
    def loop():
        while True:
            time.sleep(0.09)
            try:
                if not app.is_running:
                    return
                app.invalidate()
            except Exception:
                return
    threading.Thread(target=loop, daemon=True).start()


# ── arrow-key selector ───────────────────────────────────────────────────────
def select(title, rows, current=None):
    """rows: [(label, value)] -> value, or None if cancelled. A value of None is a
    heading. Type to filter: with a hundred sessions or thirty models, arrowing
    through the list is not a way to find anything.

    Built on prompt_toolkit rather than raw terminal mode: two different pieces of
    code driving termios in the same process is how the arrows stopped working. It
    renders inline (no full screen) and disappears when done.
    """
    if not sys.stdin.isatty() or not rows:
        return None

    flt = [""]
    view = [list(rows)]
    idx = [0]

    def refilter(keep=None):
        f = flt[0].lower()
        view[0] = ([r for r in rows if r[1] is not None and f in r[0].lower()]
                   if f else list(rows))
        pick = [i for i, (_l, v) in enumerate(view[0]) if v is not None]
        if not pick:
            idx[0] = 0
            return
        if keep is not None:
            idx[0] = next((i for i, (_l, v) in enumerate(view[0]) if v == keep), pick[0])
        idx[0] = idx[0] if idx[0] in pick else pick[0]

    refilter(current)
    if not any(v is not None for _l, v in view[0]):
        return None

    def height():
        return min(max(len(view[0]), 1), max(5, term_rows() - 8))

    def step(delta):
        pick = [i for i, (_l, v) in enumerate(view[0]) if v is not None]
        if not pick:
            return
        here = pick.index(idx[0]) if idx[0] in pick else 0
        idx[0] = pick[(here + delta) % len(pick)]

    frame = [0]

    def render():
        frame[0] += 1
        h = height()
        rws = view[0]
        top = max(0, min(idx[0] - h // 2, len(rws) - h))
        hint = "   ↑/↓ choose · enter select · type to filter · esc cancel\n"
        parts = [("class:sel.title", f"  {title}"), ("class:sel.hint", hint)]
        if flt[0]:
            parts.append(("class:mark", f"  filter: {flt[0]}"))
            parts.append(("class:sel.hint", f"   {len(rws)} match"
                                            f"{'' if len(rws) == 1 else 'es'}\n"))
        if not rws:
            parts.append(("class:sel.off", "    nothing matches\n"))
        for i in range(top, min(top + h, len(rws))):
            label = rws[i][0][:cols() - 4]
            if rws[i][1] is None:
                parts.append(("class:sel.head", f"  {label}\n"))
            elif i == idx[0]:
                parts.append((f"fg:{breathe(frame[0])} bold", "  ❯ "))
                parts += shimmer_frags(label, frame[0], base="#e4e4e4",
                                       band=COMET, speed=1, gap=10)
                parts.append(("", "\n"))
            else:
                parts.append(("class:sel.off", f"    {label}\n"))
        if len(rws) > h:
            parts.append(("class:sel.hint", f"  {idx[0] + 1}/{len(rws)}"))
        return parts

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _(e):
        step(-1)

    @kb.add("down")
    @kb.add("c-n")
    def _(e):
        step(1)

    @kb.add("pageup")
    def _(e):
        idx[0] = max(0, idx[0] - height())

    @kb.add("pagedown")
    def _(e):
        idx[0] = min(max(0, len(view[0]) - 1), idx[0] + height())

    @kb.add("enter")
    def _(e):
        rws = view[0]
        if rws and rws[idx[0]][1] is not None:
            e.app.exit(result=rws[idx[0]][1])

    @kb.add("backspace")
    def _(e):
        if flt[0]:
            flt[0] = flt[0][:-1]
            refilter()

    @kb.add(Keys.Any)
    def _(e):
        ch = e.data
        if ch and ch.isprintable():
            flt[0] += ch
            refilter()

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _(e):
        if flt[0]:                       # esc clears the filter before it gives up
            flt[0] = ""
            refilter()
            return
        e.app.exit(result=None)

    app = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(render, focusable=True),
                                     dont_extend_height=True)])),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
        erase_when_done=True,          # the dialog disappears once you choose
        style=Style.from_dict({
            "sel.title": "bold",
            "sel.hint": "fg:#585858",
            "sel.off": "fg:#a8a8a8",
            "sel.head": "fg:#585858",
        }),
    )
    return run_dialog(app)



# ── effort slider ────────────────────────────────────────────────────────────
def effort_slider(levels, current):
    """A horizontal gauge: ←/→ to move, enter to set, esc to cancel.

    levels: [(name, thinking_tokens, blurb)]. Returns a name, or None.
    """
    if not sys.stdin.isatty():
        return None
    names = [n for n, _b, _d in levels]
    cur = names.index(current) if current in names else 0
    idx = [cur]
    frame = [0]

    def render():
        frame[0] += 1
        w = min(cols() - 10, 62)
        n = len(levels)
        at = [round(i * (w - 1) / (n - 1)) for i in range(n)]

        marker = at[idx[0]]
        labels = [" "] * w
        spans = []
        for i, (name, _b, _d) in enumerate(levels):
            start = max(0, min(w - len(name), at[i] - len(name) // 2))
            spans.append((start, start + len(name), i))
            for j, ch in enumerate(name):
                labels[start + j] = ch

        name, budget, blurb = levels[idx[0]]
        pad = "    "
        head = f"{pad}{'Faster':<{w - 7}}Smarter"

        # the track: a cool-to-warm gradient with a spark running along it, the
        # setting you have now marked ┆, and a breathing ▲ under the cursor
        track = gradient_track(w, frame[0])
        track[at[cur]] = (track[at[cur]][0], "┆")
        mark_colour, mark_kind = LEVEL_STYLE[min(idx[0], len(LEVEL_STYLE) - 1)]
        if mark_kind == "flat":
            track[marker] = (f"fg:{mark_colour}", "▲")
        elif mark_kind == "wave":
            track[marker] = (f"fg:{RAMP[frame[0] % len(RAMP)]} bold", "▲")
        else:
            track[marker] = (f"fg:{breathe(frame[0], mark_colour)} bold", "▲")
        track_frags = [("class:slider.labels", pad)] + track + [("", "\n")]

        # the labels: the chosen one styled by its level, the rest quiet
        lo, hi, _ = spans[idx[0]]
        label_frags = [("class:slider.labels", pad + "".join(labels[:lo]))]
        label_frags += level_frags("".join(labels[lo:hi]), idx[0], frame[0])
        label_frags.append(("class:slider.labels", "".join(labels[hi:]) + "\n\n"))

        return [
            ("class:sel.title", "  Effort\n\n"),
            ("class:sel.head", head + "\n"),
            *track_frags,
            *label_frags,
            ("class:slider.labels", pad),
            *level_frags(name, idx[0], frame[0]),
            ("class:sel.head", f"  {tok(budget)} thinking tokens · {blurb}\n\n"),
            ("class:sel.head", "  ←/→ choose · enter set · esc cancel"),
        ]

    kb = KeyBindings()

    @kb.add("left")
    @kb.add("up")
    @kb.add("h")
    def _(e):
        idx[0] = max(0, idx[0] - 1)

    @kb.add("right")
    @kb.add("down")
    @kb.add("l")
    def _(e):
        idx[0] = min(len(levels) - 1, idx[0] + 1)

    @kb.add("enter")
    def _(e):
        e.app.exit(result=names[idx[0]])

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    @kb.add("q")
    def _(e):
        e.app.exit(result=None)

    app = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(render, focusable=True),
                                     dont_extend_height=True)])),
        key_bindings=kb, full_screen=False, mouse_support=False,
        erase_when_done=True,          # the dialog disappears once you choose
        style=Style.from_dict({
            "sel.title": "bold",
            "sel.head": "fg:#585858",
            "slider.track": "fg:#8a8a8a",
            "slider.labels": "fg:#8a8a8a",
        }),
    )
    return run_dialog(app)


class Spin:
    """Spinner state for the toolbar. prompt_toolkit draws it; we just supply
    the glyph, the activity, and the clock. Fighting the terminal with raw ANSI
    to pin a footer does not survive scrolling: this does."""

    GLYPHS = "✻✽✢✳✶✷✸✹"
    SHADES = ["#af87ff", "#d787ff", "#ff87d7", "#ff87ff", "#d787ff", "#af87ff"]

    def __init__(self):
        self.busy = False
        self.text = ""
        self.tokens = 0
        self.start = 0.0
        self.i = 0

    def begin(self, text=""):
        if not self.busy:
            self.start, self.tokens = time.monotonic(), 0
        self.busy = True
        self.text = text or self.text

    def update(self, text=None, tokens=None):
        if text is not None:
            self.text = text
        if tokens is not None:
            self.tokens = tokens

    def end(self):
        self.busy = False
        self.text = ""

    def frame(self):
        self.i += 1
        g = self.GLYPHS[self.i % len(self.GLYPHS)]
        col = self.SHADES[(self.i // 2) % len(self.SHADES)]
        secs = int(time.monotonic() - self.start) if self.start else 0
        clock = f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s"
        meta = clock
        if self.tokens:
            meta += f" · ↓ {tok(self.tokens)} tokens"
        return g, col, (self.text or "working"), meta

    def shimmer(self, text):
        return shimmer_frags(text, self.i)


# ── the listener: prints normal terminal lines ──────────────────────────────
class Printer(Listener):
    def __init__(self, app):
        self.app = app
        self.think_buf = ""
        self.think_start = 0.0
        self.line_buf = ""
        self.first_text = True
        self.at_start = True
        self.answer = ""
        self.last_answer = ""
        self.blank = True
        self.md = Markdown(self._render)
        self.live = app.spin

    def _tick(self):
        if self.app.stop:
            raise KeyboardInterrupt

    def _p(self, line=""):
        """One line, and never a blank line under a blank line.

        Blocks are separated by exactly one blank row. The model's own answers
        arrive with their markdown spacing intact (a heading, two newlines, a
        paragraph), and tool output brings its own, so without collapsing here
        those runs stack up and the transcript drifts apart into empty rows.
        """
        if not line.strip():
            if self.blank:
                return
            out("")
            self.blank = True
            return
        out(line)
        self.blank = False

    def _gap(self):
        """A blank line between blocks, but never two in a row."""
        if not self.blank:
            self._p()

    def on_thinking(self, delta):
        self._tick()
        if not self.think_buf:
            self.think_start = time.monotonic()
            self.live.begin("thinking")
        self.think_buf += delta
        if self.app.show_thinking:
            sys.stdout.write(dim(delta))
            sys.stdout.flush()
            return
        last = [unmark(l) for l in self.think_buf.splitlines() if unmark(l)]
        self.live.update(text=last[-1] if last else "thinking",
                         tokens=max(1, len(self.think_buf) // 4))

    def on_thinking_done(self):
        if not self.think_buf:
            self.live.end()
            return
        secs = int(time.monotonic() - self.think_start)
        toks = max(1, len(self.think_buf) // 4)
        last = [unmark(l) for l in self.think_buf.splitlines() if unmark(l)]
        preview = (last[-1] if last else "thought")[:max(10, cols() - 30)]
        self._gap()
        self._p(f"{magenta('✻')} {dim(preview)}  "
                f"{dim(f'[{secs}s · ~{toks} tok · ctrl-t]')}")
        self.think_buf = ""

    def on_text(self, delta):
        """Line buffered. A line is printed only once it is complete, so nothing
        is ever rewritten: rewriting breaks the moment a line wraps."""
        self._tick()
        if self.first_text:
            self.on_thinking_done()      # the ✻ summary belongs above the answer
            self._gap()
            self.first_text = False
        self.answer += delta
        parts = delta.split("\n")
        for part in parts[:-1]:
            self.line_buf += part
            self._emit_line(self.line_buf)
            self.line_buf = ""
        self.line_buf += parts[-1]

    def _emit_line(self, line):
        """Feed the line to the markdown renderer, which prints it (holding back
        tables and fenced code until the block closes)."""
        self.md.feed(line)

    def _render(self, text, raw=False):
        """One finished line from the markdown renderer. Prose arrives raw so it
        can be wrapped BEFORE it is styled (styling first would break the wrap
        and strip the markers). Tables and code arrive already laid out."""
        if not text.strip():
            self._p()
            return
        if not raw:
            mark = f"{green('⏺')} " if self.at_start else "  "
            self.at_start = False
            self._p(mark + text)
            return
        for i, chunk in enumerate(textwrap.wrap(text, width=max(30, cols() - 4)) or [""]):
            mark = f"{green('⏺')} " if (self.at_start and i == 0) else "  "
            self.at_start = False
            self._p(mark + inline(chunk))

    def on_text_done(self):
        if self.line_buf:
            self._emit_line(self.line_buf)
            self.line_buf = ""
        self.md.close()
        if self.answer.strip():
            self.last_answer = self.answer
        self.answer = ""
        self.first_text = True
        self.at_start = True
        sys.stdout.flush()

    def on_tool_call(self, name, args):
        self._tick()
        self.on_thinking_done()          # the summary belongs above the call
        self._gap()
        self._p(f"{green('⏺')} {bold(LABEL.get(name, name))}({dim(arg_of(name, args))})")
        self.live.begin(f"running {LABEL.get(name, name).lower()}")

    def on_tool_result(self, name, result):
        self.live.update(text="thinking")
        lines = result.splitlines() or ["(no output)"]
        for i, ln in enumerate(lines[:6]):
            prefix = f"  {dim('⎿')}  " if i == 0 else "     "
            self._p(prefix + dim(ln[:cols() - 8]))
        if len(lines) > 6:
            self._p(dim(f"     … +{len(lines) - 6} more lines"))

    def confirm(self, reason, name, args):
        """Runs on the WORKER thread. It must not read stdin: the main thread is
        inside prompt_toolkit's prompt, and two readers on one terminal is what
        broke the arrow keys and swallowed the answer here. Hand the question to
        the prompt and wait for it."""
        self.live.end()
        self._gap()
        self._p(f"{yellow('⚠')} {bold(LABEL.get(name, name))}  {dim(arg_of(name, args))}")
        self._p(f"  {yellow(reason)}")
        return self.app.request_confirm(name)

    def end_of_turn(self):
        self._gap()

    def on_status(self, state):
        """Retries, tool progress, compaction: show it in the footer instead of
        letting it disappear."""
        if state and state not in ("idle", "thinking", "writing"):
            self.live.update(text=state)

    def on_steer(self, text):
        out(cyan("↩ your message went in, it is reading it now"))

    def on_compaction(self, before):
        out(dim(f"⧉ context is full ({tok(before)}), compacting"))

    def on_error(self, message):
        out(red(f"✖ {message}"))

    def stop_requested(self):
        return self.app.stop


RESTART = "\x00restart"        # sentinel: redraw the prompt at a new height

PASTE = re.compile(r"\[paste #(\d+) · (\d+) lines[^\]]*\]")
PASTE_MIN_LINES = 3          # 1 or 2 lines is just typing; more is an attachment
BURST = 0.02                 # keys this close together are a paste, not a typist
SETTLE = 0.06                # wait this long before deciding an Enter was really Enter

KEYLOG = os.environ.get("SESAME_KEYLOG")


def klog(msg):
    """SESAME_KEYLOG=/tmp/keys.log ./run.sh  -> what the prompt actually received."""
    if not KEYLOG:
        return
    try:
        with open(KEYLOG, "a", encoding="utf-8") as f:
            f.write(f"{time.monotonic():9.4f}  {msg}\n")
    except OSError:
        pass


def paste_label(pid, text):
    lines = text.count("\n") + 1
    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    preview = first[:32] + ("…" if len(first) > 32 else "")
    return f"[paste #{pid} · {lines} lines · {preview}]"


COMPACT_HINT_AT = 0.85       # nudge to /compact once context passes this
DIALOG_COMMANDS = {"/model", "/provider", "/resume", "/effort"}  # open a picker; not loopable

COMMANDS = ["/help", "/goal", "/loop", "/team", "/model", "/provider", "/tools", "/undo", "/compact",
            "/effort", "/think", "/save", "/resume", "/sessions", "/memory", "/permissions",
            "/confirm", "/init", "/copy", "/clear", "/quit"]

HELP_ROWS = [
    ("/goal <objective>", "keep working toward it until done  (pause | resume | clear)"),
    ("/loop <interval> <x>", "re-run a prompt or /command on an interval  (stop)"),
    ("/team", "specialists that review the work between turns  (add | objective | task | fire)"),
    ("/model", "models, saved profiles, your own endpoint: all in one picker"),
    ("/provider", "switch provider"),
    ("/tools", "what it can do"),
    ("/undo [n]", "revert files it edited"),
    ("/compact", "free up context"),
    ("/effort <l>", "low | medium | high | max"),
    ("/think", "show or hide the full reasoning"),
    ("/save [name]", "name this session"),
    ("/resume", "pick up an earlier session"),
    ("/sessions", "list them"),
    ("/memory", "what it remembers"),
    ("/permissions", "what it may do unasked  (/permissions reset)"),
    ("/confirm on|off", "prompts before dangerous actions"),
    ("/init", "write an AGENTS.md for this project"),
    ("/copy", "copy the last answer"),
    ("/clear", "start a new conversation"),
    ("/quit", "exit"),
]
KEY_ROWS = [
    ("enter", "send · while it works, your message steers it"),
    ("esc", "stop the current turn"),
    ("ctrl-t", "show or hide the full reasoning"),
    ("ctrl-y", "copy the last answer"),
    ("paste", "3+ lines becomes one object; backspace removes the whole paste"),
    ("ctrl-c", "quit"),
    ("scroll / ⌘C", "your terminal's own scrollback and copy: nothing is captured"),
]

INIT_TEMPLATE = """# AGENTS.md

Instructions for agents working in this project.

## Commands
- build:
- test:
- lint:

## Conventions
-

## Don't touch
-
"""


class App:
    def __init__(self, cfg, resume=None):
        self.cfg = cfg
        self.loop = Loop(cfg)
        self.spin = Spin()
        self.busy = False
        self.stop = False
        self.pending = None          # a permission question waiting on you
        self._out_lock = threading.Lock()   # so parallel team members don't interleave their reports
        self._carry = ""             # text you had typed when the prompt restarted
        self.printer = Printer(self)
        self.show_thinking = cfg._raw.get("showThinking", False)
        self._compact_hinted = False
        self.session_name = None
        self.pastes = {}             # id -> the text you pasted
        self.paste_n = 0
        self.last_key = 0.0          # when the last character arrived
        self.burst_at = None         # where the current burst of characters began
        self.burst_id = 0            # so an old timer cannot close a new burst
        self.key_seq = 0             # every key bumps it; a paste keeps bumping it
        self.hist = Path.home() / ".sesame" / "history"
        self.hist.parent.mkdir(parents=True, exist_ok=True)
        self.session = self._make_session()
        if resume:
            self._resume(resume)
        else:
            self.loop.open(f"chat-{time.strftime('%m%d-%H%M%S')}")

    def _keys(self):
        kb = KeyBindings()

        @kb.add("c-t")
        def _(event):
            self.show_thinking = not self.show_thinking
            out(dim(f"reasoning {'shown' if self.show_thinking else 'collapsed'}"))

        @kb.add("c-y")
        def _(event):
            self.copy_last()

        @kb.add(Keys.BracketedPaste)
        def _(event):
            klog(f"BRACKETED PASTE {len(event.data)} bytes")
            """A pasted file becomes one object in the input, not fifty lines of it.

            This is the clean path, used by terminals that wrap a paste in markers
            (iTerm2, most Linux terminals). Terminal.app does not, so the keys below
            catch it by timing instead.
            """
            self._take_paste(event.current_buffer, event.data)

        @kb.add(Keys.Any)
        def _(event):
            """Every ordinary character. Ones that arrive in a burst are a paste."""
            buf = event.current_buffer
            now = time.monotonic()
            self.key_seq += 1
            klog(f"char {event.data!r} queued={len(event.app.key_processor.input_queue)} "
                 f"gap={now - self.last_key:.4f} burst_at={self.burst_at}")
            if now - self.last_key > BURST:      # a fresh keystroke: any burst is over
                self._end_burst(buf)
                self.burst_at = buf.cursor_position
            self.last_key = now
            buf.insert_text(event.data)
            self._arm_burst(event.app)

        @kb.add("enter")
        def _(event):
            """Is this Enter the end of your message, or a line break inside a paste?

            Terminal.app tells you nothing: it sends no bracketed-paste markers, it
            sends CR for every line break in the paste (so a pasted line break arrives
            as this exact key, not as text), and it delivers the paste in chunks, so at
            a chunk boundary there is nothing queued behind the key to give it away.

            What separates the two is only what happens next: a paste keeps coming. So
            put the newline in, wait a moment, and if nothing else arrived, and this
            burst holds no other line breaks, then it was you pressing Enter.
            """
            buf = event.current_buffer
            self.key_seq += 1
            klog(f"ENTER queued={len(event.app.key_processor.input_queue)} "
                 f"gap={time.monotonic() - self.last_key:.4f} burst_at={self.burst_at} "
                 f"text={buf.text[:40]!r}")
            self.last_key = time.monotonic()
            if self.burst_at is None:
                self.burst_at = buf.cursor_position
            buf.insert_text("\n")
            self._arm_burst(event.app)

            mark, app = self.key_seq, event.app

            async def settle():
                await asyncio.sleep(SETTLE)
                if mark != self.key_seq:
                    return                    # more keys arrived: this was a paste
                b = app.current_buffer
                start = self.burst_at
                in_paste = (start is not None
                            and "\n" in b.text[start:max(start, b.cursor_position - 1)])
                if in_paste:                  # a paste that ended on a line break
                    self._end_burst(b)
                    app.invalidate()
                    return
                if b.text.endswith("\n"):     # you pressed Enter: take it back, send
                    b.text = b.text[:-1]
                    b.cursor_position = len(b.text)
                self._end_burst(b)
                b.validate_and_handle()

            try:
                app.create_background_task(settle())
            except Exception:                 # no loop running: behave like a plain Enter
                if buf.text.endswith("\n"):
                    buf.text = buf.text[:-1]
                    buf.cursor_position = len(buf.text)
                self._end_burst(buf)
                buf.validate_and_handle()

        @kb.add("backspace")
        def _(event):
            """Backspace on a paste removes the whole paste, not one character of
            its label."""
            buf = event.current_buffer
            before = buf.document.text_before_cursor
            m = None
            for hit in PASTE.finditer(before):
                if hit.end() == len(before):
                    m = hit
            if m:
                buf.delete_before_cursor(len(m.group(0)))
                self.pastes.pop(int(m.group(1)), None)
            else:
                buf.delete_before_cursor(1)

        @kb.add("escape", eager=True)
        def _(event):
            if self.pending:
                self._answer_confirm("n")
            elif self.busy:
                self.stop = True
                out(dim("stopping"))

        return kb

    def _make_session(self):
        """The prompt: input line, the rule above it, the status below it."""
        return PromptSession(
            history=FileHistory(str(self.hist)),
            completer=WordCompleter(COMMANDS, sentence=True),
            complete_while_typing=True,
            key_bindings=self._keys(),
            multiline=False,
            reserve_space_for_menu=0,
            # erase_when_done: otherwise prompt_toolkit echoes its whole
            # rendering into the scrollback, rules and all. We print the echo.
            erase_when_done=True,
            bottom_toolbar=self._toolbar,
            style=Style.from_dict({
                "bottom-toolbar": "noreverse bg:default",
                "rule": "fg:#585858",
                "status": "fg:#8a8a8a",
                "dim": "fg:#585858",
                "think": "fg:#8a8a8a",
                "warn": "fg:#d7af00 bold",
                "mark": "fg:#00afaf bold",
            }),
        )

    # ── the box: bottom border + status, always under the input ──────────────
    def _width(self):
        return max(30, min(cols() - 2, 100))

    def _prompt_text(self):
        """The line above the input.

        Idle it is a plain rule; while it works the spinner lives ON that line,
        with the rule trailing off to the right. The height must stay constant:
        prompt_toolkit pins its toolbar to the bottom of the screen, so a prompt
        that grows and shrinks leaves stranded blank rows behind.
        """
        w = self._width()
        if self.pending:
            name = self.pending["name"]
            return FormattedText([
                ("", "\n"),
                ("class:warn", f"⚠ allow {name}?  "),
                ("class:dim", "y yes · a always · n no  (esc denies)"),
                ("", "\n"),
                ("class:rule", "─" * w + "\n"),
                ("class:mark", "❯ "),
            ])
        # Idle this is 2 rows, working it is 5 (blank, spinner, blank, rule, input).
        # The height therefore CHANGES, and prompt_toolkit cannot reflow a running
        # prompt into a different height cleanly: _redraw(full=True) ends it and
        # starts a new one instead, carrying your typed text over.
        if not self.busy:
            return FormattedText([("class:rule", "─" * w + "\n"),
                                  ("class:mark", "❯ ")])

        g, col, text, meta = self.spin.frame()
        tail = f"({meta} · esc to stop)"
        head = text[:max(10, w - len(tail) - 6)]
        return FormattedText([
            ("", "\n"),                                  # blank above it
            (f"fg:{col} bold", f"{g} "),
            ("class:think", f"{head} "),
            ("class:dim", f"{tail}"),
            ("", "\n\n"),
            ("class:rule", "─" * w + "\n"),
            ("class:mark", "❯ "),
        ])

    def _toolbar(self):
        """What sits below the input: the rule and the static status."""
        st = self.loop.stats
        w = self.cfg.context_window
        pct = (st.context_tokens / w * 100) if w else 0
        used = f"{tok(st.context_tokens)}/{tok(w)} ({pct:.0f}%)"
        cost = f"${st.cost_usd:.4f}" if st.cost_usd >= 0.0001 else "$0"
        name = f"{self.session_name} · " if self.session_name else ""
        prof = f"{self.cfg.profile} · " if self.cfg.profile else ""
        info = f"{name}{prof}{self.cfg.model} · {used} · {cost} · turn {st.turns}"
        g = self.loop.goal
        if g and g.status in ("active", "paused"):
            info += f"  ·  ⊙ goal[{g.status[:4]}] t{g.turns}"
        if self.loop.loop_job:
            days = (self.loop.loop_job.expires_in() + 86399) // 86400
            info += f"  ·  ↻ loop {self.loop.loop_job.count}× ({days}d left)"
        if self.loop.team.members:
            watching = len(self.loop.team.watchers())
            info += f"  ·  ⬢ team {len(self.loop.team.members)}"
            if watching:
                info += f" ({watching} watching)"
        hint = "esc stop · type to steer" if self.busy else "enter send · / commands"
        return FormattedText([("class:rule", "─" * self._width() + "\n"),
                              ("class:status", f"  {info}"),
                              ("class:dim", f"   ·   {hint}")])

    @staticmethod
    def _cursor_row():
        """Ask the terminal where the cursor is (CPR). None if it will not say."""
        if not (sys.stdin.isatty() and TTY):
            return None
        fd = sys.stdin.fileno()
        try:
            saved = termios.tcgetattr(fd)
        except termios.error:
            return None
        try:
            tty.setcbreak(fd)
            sys.stdout.write("\x1b[6n")
            sys.stdout.flush()
            # os.read, not sys.stdin.read: the buffered text stream blocks
            # waiting to fill its buffer even when select says data is ready
            buf = b""
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                if not _select.select([fd], [], [], 0.05)[0]:
                    continue
                buf += os.read(fd, 64)
                if b"R" in buf:
                    break
            m = re.search(rb"\x1b\[(\d+);(\d+)R", buf)
            return int(m.group(1)) if m else None
        except OSError:
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def _to_bottom(self):
        park_bottom()

    def ask(self, question, default=""):
        """A question between turns. Returns None if you back out.

        esc has to be bound explicitly: prompt_toolkit treats a bare esc as the
        start of a meta sequence, so without this there was no way out of the
        custom endpoint flow at all.
        """
        kb = KeyBindings()

        @kb.add("escape", eager=True)
        @kb.add("c-c")
        def _(event):
            event.app.exit(result=None)

        try:
            answer = pt_prompt(HTML(f"{question}"), key_bindings=kb, default=default)
        except (EOFError, KeyboardInterrupt):
            return None
        return None if answer is None else answer.strip()

    # ── banner ───────────────────────────────────────────────────────────────
    def banner(self):
        d = os.path.basename(self.cfg.workdir) or self.cfg.workdir
        agents = f"  {dim('·')}  {green('AGENTS.md')}" if self.loop.project_files else ""
        out()
        out(f"{bold('✳ sesame agent')}")
        out(f"{cyan(self.cfg.model)}  {dim('·')}  {d}{agents}")
        out(dim("type a task, or / for commands"))
        out()

    # ── one turn, in the background: the prompt stays at the bottom ──────────
    def turn(self, text):
        self.printer.blank = False       # so the first block gets a gap after ❯
        self.busy, self.stop = True, False
        self.spin.begin("working")
        self._redraw(full=True)          # the prompt grows: reflow
        threading.Thread(target=self._work, args=(text,), daemon=True).start()

    def _background(self, label, fn, done=None):
        """Run a slow command (compaction, summarize) off the main thread with the
        spinner, so the prompt never freezes and esc can stop it."""
        self.printer.blank = False
        self.busy, self.stop = True, False
        self.spin.begin(label)
        self._redraw(full=True)

        def work():
            try:
                result = fn()
                if done:
                    out(done(result))
            except KeyboardInterrupt:
                out(dim("stopped"))
            except Exception as exc:
                out(red(f"✖ {exc}"))
            finally:
                self.busy = False
                self.spin.end()
                self._redraw(full=True)

        threading.Thread(target=work, daemon=True).start()

    # ── goal & loop (Codex /goal, Claude Code /loop) ─────────────────────────
    def _cmd_goal(self, arg):
        a = arg.strip()
        g = self.loop.goal
        if not a:
            if not g:
                out(dim("no goal. /goal <objective> to set one, and it keeps working toward it"))
                return
            used = g.used(self.loop.stats.output_tokens)
            meter = f" · {tok(used)}/{tok(g.budget)} tok" if g.budget else f" · {tok(used)} tok"
            out(f"{bold('goal')} [{g.status}] · turn {g.turns}{meter}")
            out(dim(f"  {g.objective}"))
            return
        if a == "pause":
            self.loop.goal_pause(); out(dim("goal paused")); return
        if a == "clear":
            out(dim("goal cleared") if self.loop.goal_clear() else dim("no goal")); return
        if a == "resume":
            if not self.loop.goal_resume():
                out(dim("no paused goal to resume")); return
            nxt = self.loop.goal_next()
            if nxt:
                out(dim("resuming goal…")); self.turn(nxt)
            return
        self.loop.set_goal(a)
        out(green(f"⊙ goal set — I will keep working until it is done:"))
        out(dim(f"  {a}"))
        self.turn(a)                     # start pursuing right away

    def _cmd_loop(self, arg):
        a = arg.strip()
        j = self.loop.loop_job
        if not a:
            if not j:
                out(dim("no loop. /loop [interval] <prompt>, e.g. /loop 5m check the build"))
                return
            days = (j.expires_in() + 86399) // 86400
            out(f"{bold('loop')} every {j.interval}s · ran {j.count}× · expires in {days}d")
            out(dim(f"  {j.prompt}"))
            return
        if a in ("stop", "clear", "off"):
            out(dim("loop stopped") if self.loop.loop_clear() else dim("no loop")); return
        # format is: /loop <interval> <prompt-or-/command>. The interval is optional
        # (defaults to 10m); the target can be a plain prompt or a slash command.
        parts = a.split(None, 1)
        secs = goals.parse_interval(parts[0])
        if secs is not None:
            if len(parts) < 2:
                out(dim("what should it run? /loop <interval> <prompt or /command>")); return
            prompt = parts[1]
        else:
            secs, prompt = goals.DEFAULT_LOOP_SECONDS, a
        if prompt.startswith("/") and prompt.split()[0] in DIALOG_COMMANDS:
            out(dim(f"{prompt.split()[0]} opens a picker and cannot be looped")); return
        self.loop.set_loop(secs, prompt)
        out(green(f"↻ loop set — every {secs}s:"))
        out(dim(f"  {prompt}"))
        self._fire_loop(prompt)          # run the first one now; the scheduler does the rest

    def _fire_loop(self, prompt):
        """A loop target can be a prompt (run as a turn) or a slash command like
        /compact (run as that command), matching /loop 5m /foo."""
        if prompt.startswith("/"):
            self.command(prompt)
        else:
            self.turn(prompt)

    def _scheduler(self):
        """Fire a due loop job when idle. app.exit(RESTART) inside turn() re-draws the
        prompt from here the same way a finished turn does, so this is safe from a
        background thread."""
        while True:
            time.sleep(2)
            if self.busy or self.pending or not self.loop.loop_job:
                continue
            try:
                typing = self.session.app.current_buffer.text.strip()
            except Exception:
                typing = ""
            if typing:                   # do not interrupt something you are typing
                continue
            if self.loop.loop_due(time.monotonic()):
                j = self.loop.loop_job
                j.fired(time.monotonic())
                self.loop._save()
                if j.expired():                  # 7 days on: fire one last time, then delete
                    out(dim(f"↻ loop #{j.count} (final — expired after 7 days)"))
                    prompt = j.prompt
                    self.loop.loop_clear()
                    self._fire_loop(prompt)
                else:
                    out(dim(f"↻ loop #{j.count}"))
                    self._fire_loop(j.prompt)

    # ── team: named specialists that review the work between turns ───────────
    def _cmd_team(self, arg):
        t = self.loop.team
        parts = arg.split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if not sub or sub == "list":
            self._team_roster()
        elif sub in ("add", "hire"):
            if not rest:
                out(dim("who? /team add <role>   e.g. /team add UI checker")); return
            m = t.add(rest)
            self.loop._save()
            col = member_color(m.name)
            out(green(f"⬢ hired {col(bold(m.name))} as {bold(m.role)}"))
            out(dim(f"    /team objective {m.name} <what to keep checking>   → a standing watcher"))
            out(dim(f"    /team task {m.name} <one-off job>                   → do it once, now"))
        elif sub in ("fire", "remove"):
            m = t.fire(rest)
            self.loop._save()
            out(dim(f"let {m.name} go") if m else dim(f"no team member named {rest}"))
        elif sub in ("objective", "watch"):
            bits = rest.split(None, 1)
            if len(bits) < 2:
                out(dim("/team objective <name> <what they should keep checking>")); return
            m = t.get(bits[0])
            if not m:
                out(dim(f"no team member named {bits[0]}")); return
            m.watch(bits[1])
            self.loop._save()
            col = member_color(m.name)
            out(green(f"⬢ {col(bold(m.name))} now reviews after every turn:"))
            out(dim(f"    {bits[1]}"))
        elif sub in ("task", "delegate"):
            bits = rest.split(None, 1)
            if len(bits) < 2:
                out(dim("/team task <name> <what to do>")); return
            m = t.get(bits[0])
            if not m:
                out(dim(f"no team member named {bits[0]}")); return
            name, task = m.name, bits[1]
            out(dim(f"⬢ {member_color(name)(name)} is on it…"))
            self._background(f"{name} working",
                             lambda: self.loop.team_task(name, task, on_event=self._member_event,
                                                         should_stop=lambda: self.stop))
        else:
            out(dim("usage: /team [list | add <role> | objective <name> <obj> | "
                    "task <name> <job> | fire <name>]"))

    def _team_roster(self):
        t = self.loop.team
        if not t.members:
            out(dim("no team yet. build one so your work gets reviewed as it happens:"))
            out(dim("    /team add UI checker"))
            out(dim("    /team objective <name> keep the UI aligned, nothing overflowing"))
            return
        watching = len(t.watchers())
        out(f"{bold('⬢ team')}  {dim(f'· {len(t.members)} members · {watching} watching')}")
        for m in t.members:
            col = member_color(m.name)
            state = green("watching") if m.watching else dim("idle")
            out(f"  {col('⬢')} {bold(m.name)} · {m.role}  [{state}]")
            if m.objective:
                out(dim(f"      objective: {m.objective}"))
            meta = f"{m.runs} reviews · stepped in {m.interventions}× · {len(m.memory)} notes"
            if m.last:
                meta += f" · last: {m.last[:60]}"
            out(dim(f"      {meta}"))

    def _member_event(self, ev):
        """Render a team member's activity: spinner while it works, a report when
        it lands. Members run in parallel during a review, so reports take a lock."""
        if self.stop:                    # esc during a review: unwind the member's run
            raise KeyboardInterrupt
        t = ev.get("type")
        if t == "member_start":
            self.spin.update(text=f"{ev.get('name', 'member')} · {ev.get('kind', '')}…")
        elif t == "tool_use":
            self.spin.update(text=f"{ev.get('member', 'member')}: "
                                  f"{LABEL.get(ev['name'], ev['name']).lower()}")
        elif t == "member_done":
            with self._out_lock:
                self._member_report(ev)

    def _member_report(self, v):
        name, role = v.get("name", "member"), v.get("role", "")
        col = member_color(name)
        flags = v.get("flags") or []
        head = f"{col('⬢')} {col(bold(name))} {dim('· ' + role)}"
        if flags:
            out(f"{head}  {yellow(f'{len(flags)} flag' + ('s' if len(flags) != 1 else ''))}")
            for f in flags:
                sev = f.get("severity", "normal")
                mark = red("●") if sev == "blocker" else yellow("●") if sev == "normal" else dim("○")
                fix = dim(f" → {f['fix']}") if f.get("fix") else ""
                where = dim(f"  [{f['where']}]") if f.get("where") else ""
                out(f"    {mark} {f['issue']}{fix}{where}")
        elif v.get("cleared"):
            out(f"{head}  {green('all clear')}")
        else:
            out(f"{head}  {dim(v.get('last') or 'no report')}")
        if v.get("kind") == "task" and v.get("summary"):
            for ln in v["summary"].splitlines():
                out(f"    {dim(ln)}")

    def _team_review(self):
        """After a turn, every watcher inspects the work; their flags drive a
        bounded set of follow-ups, so the lead fixes what they found."""
        if self.stop or not self.loop.team.watchers():
            return
        for _ in range(team.MAX_REVIEW_ROUNDS):
            watchers = self.loop.team.watchers()
            out(dim(f"⬢ team review · {len(watchers)} watching"))
            interventions = self.loop.review_round(on_event=self._member_event,
                                                   should_stop=lambda: self.stop)
            if self.stop:
                return
            if not interventions:
                out(dim("  ✓ team: all clear"))
                return
            n = sum(len(v["flags"]) for v in interventions)
            out(cyan(f"  → the lead is addressing {n} flag" + ("s" if n != 1 else "")))
            self._run_with_steers(team.compose_review(interventions))
        out(dim("  ⬢ team: review rounds used up for this turn"))

    def _run_with_steers(self, text):
        """One turn, then anything you typed while it worked, until the queue drains."""
        self.loop.run(text, self.printer)
        leftover = self.loop.pending_steer()
        while leftover and not self.stop:
            out(dim("it finished before reading your message, running it now"))
            self.loop.run(leftover, self.printer)
            leftover = self.loop.pending_steer()

    def _work(self, text):
        try:
            self._run_with_steers(text)
            # goal: keep pursuing the objective, turn after turn, until the model
            # calls goal_done, a budget runs out, or you pause or stop it.
            nxt = self.loop.goal_next()
            while nxt and not self.stop:
                out(dim(f"↻ goal · continuing (turn {self.loop.goal.turns})"))
                self._run_with_steers(nxt)
                nxt = self.loop.goal_next()
            self._report_goal()
            self._team_review()          # the board reviews what was just done
        except KeyboardInterrupt:
            out(dim("stopped"))
            if self.loop.goal and self.loop.goal.status == "active":
                self.loop.goal_pause()
                out(dim("goal paused · /goal resume to continue"))
        except Exception as exc:
            out(red(f"✖ {exc}"))
        finally:
            self.printer.end_of_turn()   # blank line before your next ❯
            self._maybe_compact_hint()
            self.busy = False
            self.spin.end()
            self._redraw(full=True)   # the prompt restarts; the loop parks it

    def _report_goal(self):
        g = self.loop.goal
        if not g or self.stop:
            return
        if g.status == "complete":
            out(green(f"✓ goal complete: {g.summary}"))
        elif g.status == "budget_limited":
            out(yellow(f"⏸ goal stopped after {g.turns} turns · /goal resume to keep going"))

    def _maybe_compact_hint(self):
        """Nudge to /compact once context is most of the way full — once per fill,
        not every turn (hysteresis: warn at 85%, re-arm once it drops below 70%)."""
        w = self.cfg.context_window
        if not w:
            return
        pct = self.loop.stats.context_tokens / w
        if pct >= COMPACT_HINT_AT and not self._compact_hinted:
            self._compact_hinted = True
            out(yellow(f"⧉ context {pct * 100:.0f}% full · /compact to free it"))
        elif pct < 0.70:
            self._compact_hinted = False

    def request_confirm(self, name):
        """Called from the worker thread. Blocks it until you answer."""
        done = threading.Event()
        was_busy, self.busy = self.busy, False   # stop the spinner while it asks
        self.pending = {"name": name, "event": done, "answer": False}
        self._redraw(full=True)
        done.wait()
        answer = self.pending["answer"]
        self.pending = None
        self.busy = was_busy
        self._redraw(full=True)
        return answer

    def _answer_confirm(self, text):
        t = text.strip().lower()
        if t in ("a", "always"):
            self.pending["answer"] = "always"
            out(green("  ✓ always allowed in this project"))
        elif t in ("", "y", "yes"):
            self.pending["answer"] = True
            out(green("  ✓ approved"))
        else:
            self.pending["answer"] = False
            out(red("  ✗ denied"))
        self.pending["event"].set()

    def _take_paste(self, buf, data):
        # Terminal.app sends CR for every line break, inside the bracketed paste as
        # well. Counting "\n" here found one line in a forty line file, so the paste
        # was declared too small to fold and went into the prompt whole. That was the
        # bug: not the markers, not the timing, this line.
        data = data.replace("\r\n", "\n").replace("\r", "\n")
        if data.count("\n") + 1 < PASTE_MIN_LINES:
            buf.insert_text(data)
            return
        self.paste_n += 1
        self.pastes[self.paste_n] = data
        buf.insert_text(paste_label(self.paste_n, data))

    def _arm_burst(self, app):
        """Close the paste a moment after the characters stop arriving, so you see the
        label right away instead of a screenful of what you pasted until you happen to
        press another key."""
        self.burst_id += 1
        mine = self.burst_id

        async def later():
            await asyncio.sleep(0.12)
            if mine != self.burst_id or self.burst_at is None:
                return
            self._end_burst(app.current_buffer)
            app.invalidate()

        try:
            app.create_background_task(later())
        except Exception:                        # no running loop: fold on the next key
            pass

    def _end_burst(self, buf):
        """Fold the characters that just poured in into one paste object."""
        start, self.burst_at = self.burst_at, None
        if start is None or buf.cursor_position <= start:
            klog(f"end_burst: nothing to fold (start={start}, cursor={buf.cursor_position})")
            return
        end = buf.cursor_position
        chunk = buf.text[start:end]
        klog(f"end_burst: {chunk.count(chr(10)) + 1} lines, {len(chunk)} chars")
        if chunk.count("\n") + 1 < PASTE_MIN_LINES:
            return
        self.paste_n += 1
        self.pastes[self.paste_n] = chunk
        label = paste_label(self.paste_n, chunk)
        buf.text = buf.text[:start] + label + buf.text[end:]
        buf.cursor_position = start + len(label)

    def expand(self, text):
        """The label is for you. The model gets what you actually pasted."""
        def swap(m):
            return self.pastes.get(int(m.group(1)), m.group(0))
        out_text = PASTE.sub(swap, text)
        self.pastes.clear()
        return out_text

    def copy_last(self):
        """The last answer, on the clipboard. Bound to ctrl-y and to /copy."""
        text = self.printer.last_answer
        if not text:
            out(dim("nothing to copy yet"))
            return
        for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b"]):
            try:
                subprocess.run(cmd, input=text, encoding="utf-8", check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                out(dim(f"copied {len(text)} characters"))
                return
            except (OSError, subprocess.SubprocessError):
                continue
        out(dim("no clipboard tool found (pbcopy, xclip, xsel)"))

    def _redraw(self, full=False):
        """full=True when the prompt changes height: the spinner block appears or
        disappears.

        A running prompt cannot change height cleanly, so end it and start a fresh
        one. Anything you had typed is carried over (_carry).

        This is only half of it. prompt_toolkit pins the toolbar to the bottom rows
        of the screen but draws the prompt wherever the cursor happens to be, so
        any free space between them becomes blank rows under ❯. A finished turn or
        a dialog that erased itself leaves exactly that much space, which is why
        the gap used to track the height of whatever just closed. park_bottom()
        pushes the cursor back down before the next prompt is drawn."""
        try:
            app = self.session.app
            if full and app.is_running:
                self._carry = app.current_buffer.text
                app.exit(result=RESTART)
            else:
                app.invalidate()
        except Exception:
            pass

    def _animate(self):
        """Keep the toolbar ticking while it works."""
        while True:
            time.sleep(0.12)
            if self.busy:
                self._redraw()

    # ── commands ─────────────────────────────────────────────────────────────
    def command(self, line):
        parts = line.split()
        cmd, arg = parts[0], " ".join(parts[1:])
        if cmd in ("/quit", "/exit"):
            return "quit"
        if cmd == "/help":
            w = max(len(c) for c, _ in HELP_ROWS + KEY_ROWS) + 2
            out()
            for c, d in HELP_ROWS:
                out(f"  {cyan(c)}{' ' * (w - len(c))}{dim(d)}")
            out()
            for k, d in KEY_ROWS:
                out(f"  {cyan(k)}{' ' * (w - len(k))}{dim(d)}")
            out()
        elif cmd == "/goal":
            self._cmd_goal(arg)
        elif cmd == "/loop":
            self._cmd_loop(arg)
        elif cmd == "/team":
            self._cmd_team(arg)
        elif cmd == "/model":
            self._pick_model(arg)
        elif cmd == "/provider":
            self._pick_provider(arg)
        elif cmd == "/tools":
            w = max(len(t["name"]) for t in self.loop.tools) + 2
            for t in self.loop.tools:
                tag = dim("auto") if t.get("read_only") else dim("ask ")
                out(f"  {bold(t['name'])}{' ' * (w - len(t['name']))}{tag}  "
                    f"{dim(t['description'][:cols() - w - 12])}")
        elif cmd == "/undo":
            turns = checkpoint.turns()
            if not turns:
                out(dim("nothing to undo"))
            else:
                n = int(arg) if arg.strip().isdigit() else turns[-1]["turn"]
                notes = checkpoint.restore(n)
                out(yellow(f"↺ undo turn {n}"))
                for note in notes or []:
                    out(dim(f"  {note}"))
        elif cmd == "/compact":
            self._compact_hinted = False
            out(dim("⧉ compacting the conversation… (this calls the model; it can take a bit)"))
            # in the background so the prompt never freezes; the spinner ticks while it works
            self._background(
                "compacting",
                lambda: self.loop.compact_now(self.printer),
                lambda did: green(f"⧉ compacted — context freed to {tok(self.loop.stats.context_tokens)}")
                if did else dim("nothing to compact yet"))
        elif cmd == "/effort":
            self._pick_effort(arg.strip().lower())
        elif cmd == "/think":
            self.show_thinking = not self.show_thinking
            out(dim(f"reasoning {'shown' if self.show_thinking else 'collapsed'}"))
        elif cmd == "/save":
            name = arg or self.session_name or "session"
            p = self.loop.save_as(name)
            self.session_name = name
            out(dim(f"saved as {name} ({p})"))
        elif cmd == "/resume":
            self._resume(arg or None)
        elif cmd == "/sessions":
            rows = tx.list_sessions()
            if not rows:
                out(dim("  no sessions yet"))
            for r in rows:
                when = time.strftime("%m-%d %H:%M", time.localtime(r["updated"]))
                meta = f"{when} · {r['turns']} turns · {r['messages']} msgs · ${r['cost']:.4f}"
                out(f"  {r['name']:<24} {dim(meta)}")
        elif cmd == "/memory":
            for ln in self.loop.memory.recall().splitlines():
                out(dim(f"  {ln}"))
        elif cmd == "/permissions":
            import project
            if arg == "reset":
                self.loop.perms = {"tools": [], "prefixes": []}
                project.save_permissions(self.loop.perms)
                out(dim("permissions reset"))
            else:
                p = self.loop.perms
                out(dim(f"  tools:    {', '.join(p['tools']) or '(none)'}"))
                out(dim(f"  prefixes: {', '.join(p['prefixes']) or '(none)'}"))
        elif cmd == "/confirm":
            if arg in ("on", "off"):
                self.cfg.confirm_danger = arg == "on"
            out(dim(f"dangerous actions {'ask first' if self.cfg.confirm_danger else 'run without asking'}"))
        elif cmd == "/init":
            p = Path("AGENTS.md")
            if p.exists():
                out(dim("AGENTS.md already exists"))
            else:
                p.write_text(INIT_TEMPLATE)
                out(dim("wrote AGENTS.md, fill it in and restart"))
        elif cmd == "/copy":
            self.copy_last()
        elif cmd == "/clear":
            self.loop.close()
            self.loop = Loop(self.cfg)
            self.printer.app = self
            self.loop.open(f"chat-{time.strftime('%m%d-%H%M%S')}")
            self.session_name = None
            os.system("clear")
            self.banner()
        else:
            out(dim("unknown command, /help"))
        return None

    # ── model / provider / custom endpoint ───────────────────────────────────
    def _pick_model(self, arg=""):
        if arg:
            self.cfg.profile = None      # you chose a model yourself
            if not self.cfg.use_model(arg):
                key = self.ask(f"  API key for {models.spec(arg)['provider']}"
                               f" (esc to cancel): ")
                if not key or not self.cfg.use_model(arg, key):
                    out(dim(f"cancelled, still using {self.cfg.model}"))
                    return
            self._model_changed()
            return
        live = models.fetch(self.cfg.base_url, self.cfg.api_key, self.cfg.api_type, timeout=6)
        source = self.cfg.active_provider if live else "built-in list"
        if not live:
            live = models.known()
        profs = self.cfg.profiles()

        rows = []
        for name, prof in sorted(profs.items()):
            mark = "●" if name == self.cfg.profile else " "
            where = prof.get("provider") or prof.get("baseUrl", "")
            win = prof.get("contextWindow")
            if win:
                where = f"{tok(int(win))} ctx · {where}"
            rows.append((f"{mark} {name:<16} {prof.get('model', '?'):<24} {where}",
                         f"__profile__{name}"))
        if profs:
            rows.append((f"── models from {source} " + "─" * 20, None))
        for m in live:
            sp = models.spec(m)
            price = (f"{tok(sp['window'])} ctx · ${sp['in']}/${sp['out']} per Mtok"
                     if sp["in"] is not None else "")
            mark = "●" if m == self.cfg.model and not self.cfg.profile else " "
            rows.append((f"{mark} {m:<36} {price}", m))
        rows.append(("＋ Use my own model      (Ollama, LM Studio, any endpoint)", "__custom__"))
        rows.append(("⇄ Switch provider…      (openai, openrouter, groq, …)", "__provider__"))
        rows.append((f"💾 Save this setup as a profile   ({self.cfg.model})", "__save__"))
        if profs:
            rows.append(("🗑  Delete a profile", "__delete__"))

        title = "profiles and models" if profs else f"models from {source}"
        here = f"__profile__{self.cfg.profile}" if self.cfg.profile else self.cfg.model
        choice = select(title, rows, current=here)
        if choice is None:
            out(dim("cancelled"))
        elif choice == "__custom__":
            self._custom()
        elif choice == "__provider__":
            self._pick_provider()
        elif choice == "__save__":
            self._save_profile()
        elif choice == "__delete__":
            self._delete_profile()
        elif choice.startswith("__profile__"):
            name = choice[len("__profile__"):]
            self.cfg.use_profile(name)
            out(dim(f"profile '{name}'"))
            self._model_changed()
        else:
            self._pick_model(choice)

    def _pick_provider(self, arg=""):
        if arg:
            name = arg.split()[0]
            key = arg.split()[1] if len(arg.split()) > 1 else ""
            if not self.cfg.switch_provider(name, key):
                key = self.ask(f"  API key for {name} (esc to cancel): ")
                if not key or not self.cfg.switch_provider(name, key):
                    out(dim(f"cancelled, still using {self.cfg.model}"))
                    return
            self._model_changed()
            return
        rows = [("＋ Custom endpoint       (your own gateway or local server)", "__custom__")]
        for n in providers.names():
            url, wire, model = providers.PRESETS[n]
            tag = "local, no key" if n in providers.LOCAL else wire
            mark = "●" if self.cfg.base_url.rstrip("/") == url.rstrip("/") else " "
            rows.append((f"{mark} {n:<14} {tag:<14} {url}", n))
        choice = select("switch provider", rows)
        if choice is None:
            out(dim("cancelled"))
        elif choice == "__custom__":
            self._custom()
        else:
            self._pick_provider(choice)

    def _custom(self):
        out()
        out(bold("use your own model") + dim("   esc to cancel"))
        out(dim("  ollama:    http://localhost:11434/v1"))
        out(dim("  lm studio: http://localhost:1234/v1"))
        url = self.ask("  base URL: ")
        if not url:
            out(dim("cancelled"))
            return
        found = models.fetch(url, "", "openai", timeout=6)
        if found:
            out(dim(f"  found {len(found)} models"))
            model = select("which model", [(m, m) for m in found])
            if model is None:
                out(dim("cancelled"))
                return
        else:
            model = self.ask("  model id: ")
            if model is None:
                out(dim("cancelled"))
                return
        key = self.ask("  API key (blank for a local server): ")
        if key is None:
            out(dim("cancelled"))
            return
        wire = "anthropic" if "anthropic" in url else "openai"
        # the window: ask the server, and only fall back to asking you. A model
        # sesame does not know would otherwise be sized at the 128k default, and
        # it would compact a 256k model at half its capacity.
        window = models.window_of(url, key, wire, model or "", timeout=6)
        if window:
            out(dim(f"  context window: {tok(window)} (from the server)"))
        else:
            typed = self.ask("  context window (blank = 128k): ")
            if typed is None:
                out(dim("cancelled"))
                return
            try:
                window = int(typed.strip().replace("_", "").replace(",", "") or 0)
            except ValueError:
                window = 0
                out(dim("  not a number, using 128k"))
        # a local server has no reasoning_effort to give: do not send one
        thinking = "none" if providers.is_local(url) else None
        self.cfg.connect(url, model or "local-model", key, wire,
                         window=window, thinking=thinking)
        self._model_changed()
        import doctor
        err = doctor._probe({"baseUrl": self.cfg.base_url, "model": self.cfg.model,
                             "apiKey": self.cfg.api_key, "wire": self.cfg.api_type})
        if err:
            out(red(f"  {err}"))
            if providers.is_local(url):
                out(dim("  is the server running?  ollama serve  ·  LM Studio → start server"))
        else:
            out(green("  connection works"))

    LEVELS = [
        ("low", 2000, "fastest, least thinking"),
        ("medium", 4000, "balanced"),
        ("high", 8000, "thinks harder on tricky work"),
        ("max", 16000, "thinks as long as it needs"),
    ]

    def _pick_effort(self, arg=""):
        if arg:
            if self.cfg.set_effort(arg):
                out(dim(f"effort {self.cfg.reasoning_effort} "
                        f"({self.cfg.thinking_budget} thinking tokens)"))
            else:
                out(dim("effort: low | medium | high | max"))
            return
        choice = effort_slider(self.LEVELS, self.cfg.reasoning_effort)
        if choice is None:
            out(dim("cancelled"))
            return
        self.cfg.set_effort(choice)
        out(dim(f"effort {choice} · {self.cfg.thinking_budget} thinking tokens"))
        self._redraw()

    # ── profiles: named setups, chosen from the same /model picker ───────────
    def _save_profile(self):
        name = self.ask("  name for this profile: ")
        if not name:
            out(dim("cancelled"))
            return
        self.cfg.save_profile(name)
        out(dim(f"saved profile '{name}': {self.cfg.model} · {self.cfg.base_url}"))
        self._render_status()

    def _delete_profile(self):
        profs = self.cfg.profiles()
        victim = select("delete which profile", [(n, n) for n in sorted(profs)])
        if not victim:
            out(dim("cancelled"))
            return
        self.cfg.delete_profile(victim)
        out(dim(f"deleted profile '{victim}'"))
        self._render_status()

    def _render_status(self):
        self._redraw()

    def _model_changed(self):
        sp = models.spec(self.cfg.model)
        price = (f"${sp['in']}/${sp['out']} per Mtok" if sp["in"] is not None else "price unknown")
        out(dim(f"{self.cfg.model} · {self.cfg.api_type} wire · "
                f"{tok(self.cfg.context_window)} ctx · {price}"))

    # ── sessions ─────────────────────────────────────────────────────────────
    def _title_of(self, row):
        """The first thing you said in that session. A filename tells you nothing,
        and with a hundred sessions the list has to be searchable."""
        try:
            for m in tx.parse(row["path"])[0]:
                if m.get("role") == "user" and isinstance(m.get("content"), str):
                    return m["content"].strip().splitlines()[0][:46]
        except (OSError, KeyError):
            pass
        return row["name"]

    def _resume(self, name=None):
        rows = sorted(tx.list_sessions(), key=lambda r: r["updated"], reverse=True)
        if not rows:
            out(dim("no sessions yet"))
            return
        if not name:
            opts = []
            for r in rows:
                when = time.strftime("%m-%d %H:%M", time.localtime(r["updated"]))
                title = self._title_of(r)
                opts.append((f"{title:<48} {when} · {r['turns']} turn"
                             f"{'' if r['turns'] == 1 else 's'}"
                             + (f" · ${r['cost']:.3f}" if r["cost"] else ""), r["name"]))
            name = select("resume a session", opts)
            if not name:
                out(dim("cancelled"))
                return
        if not self.loop.load(name):
            out(dim(f"no session called {name}"))
            return
        self.session_name = name
        out(dim(f"resumed {name}, {self.loop.stats.turns} turns"))
        self._replay()

    def _replay(self):
        """Print the loaded conversation the way it was printed when it happened.

        It used to print ONE line per block, cut at the first newline and at the
        terminal width: no tool output, no reasoning, no tables, no code blocks,
        no spacing. You resumed and the session on screen was not the session you
        left. Everything below goes through the same Printer that renders a live
        turn, so the replay and the original are the same code.
        """
        pr = self.printer
        names = {}                      # tool_use id -> tool name, for its result
        for m in self.loop.messages:
            role, c = m.get("role"), m.get("content")
            if role == "user":
                if isinstance(c, str):
                    pr._gap()
                    for i, ln in enumerate(c.splitlines() or [""]):
                        pr._p(f"{cyan('❯')} {ln}" if i == 0 else f"  {ln}")
                    continue
                for b in c or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        body = b.get("content")
                        if isinstance(body, list):
                            body = "".join(x.get("text", "") for x in body
                                           if isinstance(x, dict))
                        pr.on_tool_result(names.get(b.get("tool_use_id"), "tool"),
                                          str(body or ""))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        # a steering note you typed mid-turn: it is addressed to the
                        # model, so show what you actually typed, not the wrapper
                        note = b["text"].split("]\n", 1)[-1].strip()
                        pr._gap()
                        pr._p(f"{cyan('↩')} {dim(note)}")
            elif role == "assistant" and isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    kind = b.get("type")
                    if kind == "thinking":
                        self._replay_thinking(b.get("thinking", ""))
                    elif kind == "text" and b.get("text", "").strip():
                        pr.on_text(b["text"])
                        pr.on_text_done()
                    elif kind == "tool_use":
                        names[b.get("id")] = b.get("name")
                        pr._gap()
                        pr._p(f"{green('⏺')} {bold(LABEL.get(b['name'], b['name']))}"
                              f"({dim(arg_of(b['name'], b.get('input') or {}))})")
        pr.live.end()
        pr._gap()

    def _replay_thinking(self, text):
        """The ✻ line, without inventing a duration: the seconds it took are not
        in the transcript, and printing 0s would be a lie."""
        if not text.strip():
            return
        pr = self.printer
        if self.show_thinking:
            pr._gap()
            for ln in text.splitlines():
                pr._p(dim(ln))
            return
        lines = [unmark(l) for l in text.splitlines() if unmark(l)]
        preview = (lines[-1] if lines else "thought")[:max(10, cols() - 30)]
        pr._gap()
        pr._p(f"{magenta('✻')} {dim(preview)}  "
              f"{dim(f'[~{max(1, len(text) // 4)} tok · ctrl-t]')}")

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self):
        self.banner()
        if tx.unclean():
            out(dim("a previous session was interrupted · /resume to pick it up"))
            out()
        threading.Thread(target=self._animate, daemon=True).start()
        threading.Thread(target=self._scheduler, daemon=True).start()
        self._to_bottom()
        while True:
            try:
                # patch_stdout: everything the agent prints appears ABOVE this
                # prompt, so the input line and the status stay at the bottom of
                # the screen for the whole turn instead of scrolling away.
                with patch_stdout(raw=True):
                    # erase_when_done (set on the session): otherwise
                    # prompt_toolkit echoes its whole rendering into the
                    # scrollback, rules and all. We print the echo ourselves.
                    carried, self._carry = self._carry, ""
                    text = self.session.prompt(self._prompt_text, default=carried)
                if text == RESTART:      # the layout changed: draw it again
                    # The prompt has just erased itself, so the cursor is back at
                    # the end of the output and we can measure it. A finished turn
                    # leaves the screen shorter than the toolbar's bottom rows, and
                    # that free space is what strands blank rows under ❯.
                    if not self.busy and not self.pending:
                        park_bottom()
                    continue
                text = text.strip()
            except KeyboardInterrupt:
                if self.busy:
                    self.stop = True
                    continue
                break
            except EOFError:
                break
            if self.pending:
                out(f"{cyan('❯')} {text}")
                self._answer_confirm(text)
                continue
            if not text:
                continue
            out(f"{cyan('❯')} {text}")      # the echo keeps the paste label
            text = self.expand(text)        # the model gets the paste itself
            if self.busy:
                if text.startswith("/"):
                    out(dim("busy: esc to stop it first"))
                    continue
                self.loop.steer(text)          # type while it works: steer it
                out(cyan("↩ queued, it will read this on its next step"))
                continue
            if text.startswith("/"):
                done = self.command(text)
                out("")                      # keep one blank above the prompt
                park_bottom()                # a dialog frees the rows it erased
                if done == "quit":
                    break
                continue
            self.turn(text)
        self.stop = True
        self.loop.close()
        out(dim("bye"))
