"""sesame agent — log.py — a debug log that never leaks your key.

Restored from the old sesame. Off unless SESAME_LOG (or /debug) sets a path.
Every line is scrubbed of `sk-…` secrets before it touches disk.
"""

import re
import time
from pathlib import Path

_KEY = re.compile(r"(sk-[A-Za-z0-9]{6})[A-Za-z0-9-_]+")
_STATE = {"path": None}


def configure(path):
    _STATE["path"] = Path(path).expanduser() if path else None
    return _STATE["path"]


def active():
    return _STATE["path"]


def redact(text):
    return _KEY.sub(r"\1…", str(text))


def write(event, detail=""):
    p = _STATE["path"]
    if not p:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} {event} {redact(detail)}".rstrip() + "\n")
    except OSError:
        pass
