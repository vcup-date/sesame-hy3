"""sesame agent — memory.py — remember/forget tools + a visible prompt block.

Ported from the old sesame, trimmed: global items live in ~/.sesame/MEMORY.md
(one "- item" per line); session items live inside the saved session file.
The block appended to the system prompt is viewable at any time with /memory —
nothing the model sees is hidden, just off the banner.
"""

import os
from pathlib import Path


def _global_path():
    p = os.environ.get("SESAME_MEMORY")
    return Path(p).expanduser() if p else Path.home() / ".sesame" / "MEMORY.md"


def _read(path):
    if not path.is_file():
        return []
    return [line.strip()[2:].strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- ")]


def _write(path, items, title):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n" + "".join(f"- {x}\n" for x in items), encoding="utf-8")


class Memory:
    def __init__(self):
        self.global_path = _global_path()
        self.global_items = _read(self.global_path)
        self.session_items = []

    def remember(self, content, scope="global"):
        content = (content or "").strip()
        if not content:
            return "[nothing to remember]"
        if scope == "session":
            self.session_items.append(content)
        else:
            scope = "global"
            self.global_items.append(content)
            _write(self.global_path, self.global_items, "sesame memory")
        return f"[remembered in {scope} memory]"

    def forget(self, match, scope="global"):
        m = (match or "").lower().strip()
        target = self.session_items if scope == "session" else self.global_items
        before = len(target)
        target[:] = [x for x in target if m not in x.lower()]
        if scope != "session":
            _write(self.global_path, self.global_items, "sesame memory")
        return f"[removed {before - len(target)} item(s) from {scope} memory]"

    def recall(self):
        g = "\n".join(f"- {x}" for x in self.global_items) or "(none)"
        s = "\n".join(f"- {x}" for x in self.session_items) or "(none)"
        return f"GLOBAL:\n{g}\n\nSESSION:\n{s}"

    def prompt_block(self):
        parts = []
        if self.global_items:
            parts.append("Persistent memory (across all sessions):\n"
                         + "\n".join(f"- {x}" for x in self.global_items))
        if self.session_items:
            parts.append("This session's memory:\n"
                         + "\n".join(f"- {x}" for x in self.session_items))
        return "\n\n".join(parts)

    def clear_session(self):
        self.session_items = []


def make_memory_tools(mem):
    # NOT read_only: a global memory item is appended to the system prompt of
    # every future session. Web content reachable via browse/websearch can ask
    # the model to "remember" something, so an unprompted write would be a
    # persistent prompt-injection channel. Memory writes always ask.
    remember = {
        "name": "remember",
        "read_only": False,
        "description": ("Save a short durable fact to memory. scope \"global\" persists across "
                        "all sessions (~/.sesame/MEMORY.md); \"session\" lasts for this session."),
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"},
                           "scope": {"type": "string", "enum": ["global", "session"]}},
            "required": ["content"],
        },
        "execute": lambda inp: {"ok": True, "content": mem.remember(inp.get("content", ""),
                                                                    inp.get("scope", "global"))},
    }
    forget = {
        "name": "forget",
        "read_only": False,
        "description": "Remove memory items containing a substring. scope \"global\" or \"session\".",
        "input_schema": {
            "type": "object",
            "properties": {"match": {"type": "string"},
                           "scope": {"type": "string", "enum": ["global", "session"]}},
            "required": ["match"],
        },
        "execute": lambda inp: {"ok": True, "content": mem.forget(inp.get("match", ""),
                                                                  inp.get("scope", "global"))},
    }
    # The model could not read its own memory: /memory was a *user* command only.
    recall = {
        "name": "recall",
        "read_only": True,
        "description": "Show what is currently in global and session memory.",
        "input_schema": {"type": "object", "properties": {}},
        "execute": lambda inp: {"ok": True, "content": mem.recall()},
    }
    return [remember, forget, recall]
