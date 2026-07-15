"""sesame agent — checkpoint.py — /undo for agent edits.

The trust blocker this removes: the agent rewrites a 500-line file, mangles it,
and there is no way back. Before any write/edit touches a file, its current
bytes are snapshotted under .sesame/checkpoints/<turn>/. /undo restores the last
turn's files; /undo <n> goes further back.

Snapshots are taken at the tool boundary (the same seam as the safety gate), so
this needs no loop involvement.
"""

import json
import shutil
import time
from pathlib import Path

DIR = Path(".sesame") / "checkpoints"
MUTATING = {"write", "edit"}
MAX_TURNS = 50
MAX_FILE_BYTES = 5 * 1024 * 1024


def _turn_dir(turn):
    return DIR / f"turn-{turn:04d}"


def snapshot(turn, name, args):
    """Called before a mutating tool runs. Records the file's prior state."""
    if name not in MUTATING:
        return
    path = Path(str((args or {}).get("path") or ""))
    if not path.name:
        return
    d = _turn_dir(turn)
    d.mkdir(parents=True, exist_ok=True)
    index = d / "index.json"
    entries = json.loads(index.read_text()) if index.is_file() else []
    key = str(path.resolve())
    if any(e["path"] == key for e in entries):
        return  # first state of this turn wins — that is what /undo restores
    entry = {"path": key, "existed": path.is_file(), "saved": None, "at": time.time()}
    if path.is_file():
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                entry["saved"] = "__too_big__"
            else:
                blob = d / f"{len(entries):03d}-{path.name}"
                shutil.copy2(path, blob)
                entry["saved"] = blob.name
        except OSError:
            entry["saved"] = "__unreadable__"
    entries.append(entry)
    index.write_text(json.dumps(entries, indent=2))


def turns():
    if not DIR.is_dir():
        return []
    out = []
    for d in sorted(DIR.glob("turn-*")):
        index = d / "index.json"
        if not index.is_file():
            continue
        try:
            entries = json.loads(index.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"turn": int(d.name.split("-")[1]), "dir": d,
                    "files": [Path(e["path"]).name for e in entries], "entries": entries})
    return out


def restore(turn):
    """Roll the recorded files back to their state before `turn`. Returns notes."""
    d = _turn_dir(turn)
    index = d / "index.json"
    if not index.is_file():
        return None
    notes = []
    for e in json.loads(index.read_text()):
        p = Path(e["path"])
        if not e["existed"]:
            if p.is_file():
                p.unlink()
                notes.append(f"deleted {p.name} (it did not exist before)")
            continue
        if e["saved"] in (None, "__too_big__", "__unreadable__"):
            notes.append(f"could not restore {p.name} ({e['saved'] or 'not saved'})")
            continue
        blob = d / e["saved"]
        if blob.is_file():
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(blob, p)
            notes.append(f"restored {p.name}")
    return notes


def prune():
    ts = turns()
    for t in ts[:-MAX_TURNS] if len(ts) > MAX_TURNS else []:
        shutil.rmtree(t["dir"], ignore_errors=True)
