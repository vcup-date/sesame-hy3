"""sesame agent — danger.py — what actually deserves a confirmation prompt.

This is the ONLY thing that prompts. Writing a file or making a directory is not
dangerous — asking about it just trains you to hit "y" without reading, which is
exactly how the prompts that matter get waved through. So the question is never
"does this tool write?" but "is this hard to undo?":

  destructive     rm -rf, dd, mkfs, shred, git reset --hard, git clean -f, DROP TABLE…
  irreversible    force push, publish, docker push, kubectl delete
  privileged      sudo, writes to /dev, recursive chmod/chown, moves into system dirs
  unrecoverable   overwriting a file too big for /undo to snapshot
  sensitive       .env, *.key, *.pem, id_rsa, .netrc — even when new
  remote code     curl | sh
  persistent      a global memory write (it enters every future system prompt)

Writing files is not gated. A new file, an overwrite, an edit — all are snapshotted
before they run, so /undo restores them, and an agent that touches many files would
otherwise drown you in prompts. The only write still worth asking about is one /undo
cannot reverse: a file larger than checkpoint's snapshot limit.
"""

import re
from pathlib import Path

import checkpoint

_PATTERNS = [
    (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b", "recursive force delete (rm -rf)"),
    (r"\brm\s+-[a-z]*r\b", "recursive delete (rm -r)"),
    (r"\brm\b[^|;&]*--(force|recursive)", "force/recursive delete (rm --force/--recursive)"),
    (r"(?:^|[;&|]\s*|\bsudo\s+)rm\s+\S", "deletes files (rm)"),
    # rm reached indirectly — the plain `rm` rule above only sees it at the start
    # of a command, so these three slipped through entirely
    (r"-exec\s+(sudo\s+)?rm\b", "deletes files (find -exec rm)"),
    (r"\bxargs\b[^|;&]*\brm\b", "deletes files (xargs rm)"),
    (r"\b(shutil\.rmtree|os\.remove|os\.unlink|\.unlink\(\))", "deletes files (python)"),
    (r"(?:^|[;&|]\s*)(unlink|rmdir)\s+\S", "deletes files"),
    # discards YOUR uncommitted work — no undo, and easy to type by accident
    (r"\bgit\s+(checkout|restore)\s+(--\s+)?[.*]", "discards uncommitted changes"),
    (r"\bgit\s+branch\s+-D\b", "force-deletes a branch"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "drops stashed work"),
    (r"\bdocker\s+(system\s+)?prune\b", "prunes docker data"),
    (r"\bgh\s+(repo|release)\s+delete\b", "deletes a repo/release"),
    (r"\bmv\s+\S+\s+/dev/null\b", "destroys a file (mv to /dev/null)"),
    (r"\bchmod\s+[0-7]{3,4}\s+/\s*$", "changes permissions on /"),
    (r">\s*[^&|\s>][^&|]*\.(db|sqlite|sqlite3|env|key|pem)\b", "overwrites a sensitive file"),
    (r"\bsudo\b", "runs as root (sudo)"),
    (r"\bdd\s+.*of=", "raw disk write (dd)"),
    (r"\bmkfs\b", "formats a filesystem (mkfs)"),
    # a real device node — /dev/null|stdout|stderr|tty are just discard/echo
    (r">\s*/dev/(?!null\b|stdout\b|stderr\b|tty\b|zero\b)[a-z]+", "writes to a device node"),
    (r"\bchmod\s+-R\b", "recursive permission change"),
    (r"\bchown\s+-R\b", "recursive ownership change"),
    (r"\bgit\s+reset\s+--hard\b", "discards changes (git reset --hard)"),
    (r"\bgit\s+clean\s+-[a-z]*f", "deletes untracked files (git clean -f)"),
    (r"\bgit\s+push\s+.*--force", "force push"),
    (r":\(\)\s*\{.*\}\s*;", "fork bomb"),
    # remote code execution — curl was covered, wget and eval were not
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?\S*(ba|z|k|fi)?sh\b", "pipes remote script to shell"),
    (r"\beval\b[^\n]*\$\(\s*(curl|wget)", "evaluates a remote script"),
    # outward-facing / irreversible: it leaves your machine
    (r"\bgit\s+push\b", "pushes to a remote"),
    (r"\b(npm|yarn|pnpm)\s+publish\b", "publishes a package"),
    (r"\btwine\s+upload\b|\bpoetry\s+publish\b", "publishes a package"),
    (r"\bdocker\s+push\b", "pushes a container image"),
    (r"\bkubectl\s+(delete|apply)\b", "changes a cluster"),
    (r"\bterraform\s+(apply|destroy)\b", "changes infrastructure"),
    (r"\btruncate\s+-s\s*0", "truncates a file"),
    (r"\bfind\b.*-delete\b", "bulk delete (find -delete)"),
    (r"\bshred\b|\bwipe\b", "irreversible wipe"),
    (r"\bmv\s+.*\s+/(bin|etc|usr|var|sys|boot)\b", "moves into a system directory"),
]


# SQL rules only apply inside an actual SQL client invocation. Matching them
# against any shell text made "brew update" and "apt update" look destructive,
# which trains reflexive approval and defeats the prompts that matter.
_SQL_CLIENT = re.compile(r"\b(psql|mysql|mariadb|sqlite3?|mongo(sh)?|clickhouse-client|duckdb)\b", re.I)
_SQL_PATTERNS = [
    (r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)\b", "drops a table/database"),
    (r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", "unfiltered DELETE (no WHERE)"),
    (r"\bUPDATE\s+\w+\s+SET\b(?!.*\bWHERE\b)", "unfiltered UPDATE (no WHERE)"),
]


# `> file` silently replaces the file. A regex cannot tell `> new.log` (fine)
# from `> shell.py` (destroys your source), so ask the filesystem: it is only
# dangerous if the target already exists with content. `>>` appends — that's fine.
_REDIRECT = re.compile(r"(?<!>)>\s*([^\s>&|;]+)")
_TEE = re.compile(r"\btee\b\s+(?!-a\b|--append\b)(?:-\S+\s+)*([^\s>&|;]+)")


def _clobbers(command):
    for rx in (_REDIRECT, _TEE):
        for target in rx.findall(command):
            if target.startswith("/dev/"):
                continue
            p = Path(target.strip("'\"")).expanduser()
            try:
                if p.is_file() and p.stat().st_size > 0:
                    return f"overwrites existing file {p.name} (shell redirect)"
            except OSError:
                continue
    return None


def check_bash(command):
    for pattern, reason in _PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return reason
    if _SQL_CLIENT.search(command):
        for pattern, reason in _SQL_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return reason
    return _clobbers(command)


def check_sql(text):
    """For a SQL string outside a shell command (kept available for callers)."""
    for pattern, reason in _SQL_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    return None


def _sensitive(p):
    # NB: pathlib gives ".env" an empty suffix (it is a dotfile, not an
    # extension), so the name has to be checked too.
    name = p.name.lower()
    return (p.suffix.lower() in (".env", ".key", ".pem", ".pfx", ".p12")
            or name.startswith(".env")
            or name in ("id_rsa", "id_ed25519", "credentials", ".netrc", ".npmrc"))


def check(name, args):
    if name == "bash":
        return check_bash(str(args.get("command", "")))
    if name in ("write", "edit"):
        p = Path(str(args.get("path", ""))).expanduser()
        if _sensitive(p):
            return f"modifies a sensitive file ({p.name})"
        # A write or edit is not gated: you asked for it, and every one is
        # snapshotted before it runs, so /undo puts the file back. Overwriting is
        # not "hard to undo", and an agent touches many files, so a prompt per file
        # is pure noise. The one file write that IS hard to undo is one too large to
        # snapshot (over checkpoint's limit): that overwrite cannot be reversed, so
        # it is the only one still worth a prompt.
        try:
            if p.is_file() and p.stat().st_size > checkpoint.MAX_FILE_BYTES:
                mb = p.stat().st_size // (1024 * 1024)
                return f"overwrites {p.name} ({mb} MB) — too large for /undo to restore"
        except OSError:
            pass
    if name == "remember" and args.get("scope", "global") != "session":
        return "adds a permanent item to every future system prompt"
    if name == "forget" and args.get("scope", "global") != "session":
        return "deletes items from permanent memory"
    return None
