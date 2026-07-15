"""sesame agent — transcript.py — the session file.

One file per session at .sesame/sessions/<name>.jsonl, append-only, one JSON
object per line:

    {"meta":  {...}}          first line: name, model, started, cwd
    {"ts": …, "msg": {...}}   every wire message, verbatim
    {"stats": {...}}          running totals, last one wins
    {"end": …}                written on a clean exit

Written the moment each message happens, so a crash loses nothing, and /resume
parses it straight back (thinking blocks and signatures intact).

Nothing in this file ever deletes a session.
"""

import json
import time
from datetime import datetime
from pathlib import Path

DIR = Path(".sesame") / "sessions"
AUTOSAVE = "_last"
def _safe(name):
    return "".join(c for c in name if c.isalnum() or c in "-_") or "session"


def _path(name):
    DIR.mkdir(parents=True, exist_ok=True)
    return DIR / f"{_safe(name)}.jsonl"


class Session:
    def __init__(self, name, model, system=""):
        self.name = name
        DIR.mkdir(parents=True, exist_ok=True)
        self.path = DIR / f"{_safe(name)}.jsonl"
        self._last_stats = None
        if not self.path.is_file():
            self._write({"meta": {"name": name, "model": model,
                                  "started": datetime.now().isoformat(timespec="seconds"),
                                  "cwd": str(Path.cwd())}})

    def _write(self, obj):
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def append(self, msg):
        self._write({"ts": time.time(), "msg": msg})

    def stats(self, stats):
        blob = json.dumps(stats, sort_keys=True)
        if blob == self._last_stats:
            return
        self._last_stats = blob
        self._write({"stats": stats})

    def end(self):
        self._write({"end": time.time()})

    def rename(self, new_name):
        target = DIR / f"{_safe(new_name)}.jsonl"
        if target == self.path:
            return target
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "meta" in rec:
                rec["meta"]["name"] = new_name
            out.append(json.dumps(rec, ensure_ascii=False))
        target.write_text("\n".join(out) + "\n", encoding="utf-8")
        old = self.path
        self.name, self.path = new_name, target
        if old.is_file():
            old.unlink()          # moved, not destroyed
        return target


def _parse_jsonl(path):
    msgs, stats, meta, clean = [], {}, {}, False
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue              # half-written tail after a crash
        if "msg" in rec:
            msgs.append(rec["msg"])
        elif "stats" in rec:
            stats = rec["stats"]
        elif "meta" in rec:
            meta = rec["meta"]
        elif "end" in rec:
            clean = True
    return msgs, stats, meta, clean


def parse(path):
    return _parse_jsonl(path)


def load(name):
    p = _path(name)
    if not p.is_file():
        return None
    msgs, stats, meta, _ = parse(p)
    return {"name": meta.get("name", name), "messages": msgs, "stats": stats, "meta": meta}


def _files():
    if not DIR.is_dir():
        return []
    return sorted(DIR.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)


def list_sessions():
    rows = []
    for p in _files():
        msgs, stats, meta, _ = parse(p)
        if not msgs:              # nothing was ever said: hide it, never delete it
            continue
        rows.append({"name": meta.get("name", p.stem), "updated": p.stat().st_mtime,
                     "turns": stats.get("turns", 0), "messages": len(msgs),
                     "cost": stats.get("cost_usd", 0.0), "path": p})
    return rows


def unclean():
    for p in _files():
        msgs, _stats, _meta, clean = parse(p)
        if msgs and not clean:
            return p, msgs
    return None


def prune_empty():
    """Deliberately does nothing.

    This used to delete session files that parsed as empty. A cleanup routine
    that deletes whatever it cannot read is data loss waiting to happen, and
    your history is not worth that risk.
    """
    return 0
