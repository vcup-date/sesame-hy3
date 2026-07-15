"""sesame agent — project.py — per-project context, config, and permissions.

The gap this closes: without project instructions you retype "tests are `just
test`, don't touch schema.gen.ts, we use tabs" every morning in every repo. And
global memory is the wrong home for it — a fact written there is injected into
every session in every project.

Layering (later wins):  install defaults → ~/.sesame/config.json → ./.sesame/config.json → env
Project instructions:   AGENTS.md | CLAUDE.md | .sesame/AGENTS.md, searched from
                        the cwd up to the git root.
"""

import json
import os
import subprocess
from pathlib import Path

HOME_DIR = Path.home() / ".sesame"
PROJECT_DIR = Path(".sesame")
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", ".sesame/AGENTS.md")
MAX_INSTRUCTIONS = 12_000


def git_root(start="."):
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start,
                           capture_output=True, encoding="utf-8", timeout=5)
        if r.returncode == 0:
            return Path(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def git_status():
    """Branch + dirty count — the model should know if the tree is already dirty."""
    try:
        b = subprocess.run(["git", "branch", "--show-current"], capture_output=True,
                           encoding="utf-8", timeout=5)
        s = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                           encoding="utf-8", timeout=5)
        if b.returncode != 0:
            return None
        branch = b.stdout.strip() or "(detached)"
        dirty = len([l for l in s.stdout.splitlines() if l.strip()])
        return f"git: {branch}" + (f" · {dirty} uncommitted file(s)" if dirty else " · clean")
    except (OSError, subprocess.SubprocessError):
        return None


def instructions():
    """The project's own instructions, nearest file wins. Cwd, then up to the git root."""
    seen, out = set(), []
    root = git_root()
    here = Path.cwd().resolve()
    dirs = [here]
    if root:
        p = here
        while p != root and p.parent != p:
            p = p.parent
            dirs.append(p)
        if root not in dirs:
            dirs.append(root)
    for d in dirs:
        for name in INSTRUCTION_FILES:
            f = d / name
            if f.is_file() and f not in seen:
                seen.add(f)
                try:
                    text = f.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                if text:
                    out.append((f, text))
    if not out:
        return None, []
    body = "\n\n".join(f"# {f.name} ({f.parent})\n{t}" for f, t in out)[:MAX_INSTRUCTIONS]
    return body, [str(f) for f, _ in out]


def load_env():
    """.env in the project (then next to the install). Restored from the old
    sesame — without it SESAME_API_KEY in a .env file was simply ignored."""
    for p in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            pass


def load_config(install_default):
    load_env()
    cfg = dict(install_default)
    for p in (HOME_DIR / "config.json", PROJECT_DIR / "config.json"):
        if p.is_file():
            try:
                cfg.update(json.loads(p.read_text()))
            except (OSError, json.JSONDecodeError):
                pass
    for env, key in (("SESAME_BASE_URL", "baseUrl"), ("SESAME_MODEL", "model"),
                     ("SESAME_WIRE", "wire"), ("SESAME_LOG", "log"),
                     ("DEEPSEEK_API_KEY", "apiKey"), ("SESAME_API_KEY", "apiKey")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def save_config(updates, scope="project"):
    d = PROJECT_DIR if scope == "project" else HOME_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / "config.json"
    cur = {}
    if p.is_file():
        try:
            cur = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            cur = {}
    cur.update(updates)
    # None means "forget this key", not "store null": that is how the active
    # profile is cleared when you pick a model by hand
    for k in [k for k, v in cur.items() if v is None]:
        del cur[k]
    p.write_text(json.dumps(cur, indent=2) + "\n")
    try:
        os.chmod(p, 0o600)  # it may hold an API key
    except OSError:
        pass
    return p


# ── profiles: named model setups, global to your machine ─────────────────────

def load_profiles():
    p = HOME_DIR / "config.json"
    if not p.is_file():
        return {}
    try:
        return dict(json.loads(p.read_text()).get("profiles") or {})
    except (OSError, json.JSONDecodeError):
        return {}


def save_profiles(profiles):
    """The profile DEFINITIONS, which are global to your machine.

    Which one is active is NOT stored here: that lives in the project config,
    next to the model it selects. Keeping the marker global and the model local
    let them drift apart, so sesame would show "local-35b" in one directory while
    actually talking to deepseek.
    """
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    p = HOME_DIR / "config.json"
    cur = {}
    if p.is_file():
        try:
            cur = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            cur = {}
    cur["profiles"] = profiles
    cur.pop("profile", None)          # migrate: the marker used to be written here
    p.write_text(json.dumps(cur, indent=2) + "\n")
    try:
        os.chmod(p, 0o600)          # profiles hold API keys
    except OSError:
        pass
    return p


# ── permissions: persisted, per-project, argument-aware ──────────────────────

def _perm_path():
    return PROJECT_DIR / "permissions.json"


def load_permissions():
    p = _perm_path()
    if not p.is_file():
        return {"tools": [], "prefixes": []}
    try:
        data = json.loads(p.read_text())
        return {"tools": list(data.get("tools") or []),
                "prefixes": list(data.get("prefixes") or [])}
    except (OSError, json.JSONDecodeError):
        return {"tools": [], "prefixes": []}


def save_permissions(perms):
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    _perm_path().write_text(json.dumps(perms, indent=2) + "\n")


def prefix_allowed(perms, name, args):
    """An explicit `tool:<argument prefix>` rule, e.g. bash:npm test.

    This is the ONLY thing that may skip a danger prompt, because you approved
    that exact command, not the tool in general.
    """
    primary = str((args or {}).get("command") or (args or {}).get("path") or "")
    for rule in perms["prefixes"]:
        tool, _, prefix = rule.partition(":")
        if tool == name and prefix and primary.startswith(prefix):
            return True
    return False


def allowed(perms, name, args):
    """Approved for ordinary use: the whole tool, or a matching prefix rule.

    Never consulted for a dangerous call. "Always allow bash" must not mean
    "and also rm -rf, silently, forever".
    """
    return name in perms["tools"] or prefix_allowed(perms, name, args)


def remember_prefix(perms, name, args):
    """The prefix rule a user would want from this call: the command's first two
    words (`npm test`), or the file's directory."""
    a = args or {}
    if a.get("command"):
        return f"{name}:{' '.join(str(a['command']).split()[:2])}"
    if a.get("path"):
        parent = str(Path(str(a["path"])).parent)
        return f"{name}:{parent if parent != '.' else str(a['path'])}"
    return None
