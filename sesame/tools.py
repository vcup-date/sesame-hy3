"""sesame agent — tools.py — the tool set.

Plain dicts: {"name", "description", "input_schema", "read_only", "execute"}.
`read_only: True` tools are auto-allowed by the REPL's safety gate.
Every execute takes one dict (the tool_use input) and returns
{"ok": bool, "content": str}.
"""

import difflib
import gzip
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

HAS_RG = shutil.which("rg") is not None
HAS_GIT = shutil.which("git") is not None
SKIP_DIRS = {".git", "node_modules", ".sesame", "dist", "build", "__pycache__", ".venv"}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"

# The REPL overrides these from sesame.config.json.
LIMITS = {"bash_timeout": 180.0, "tool_output": 16000}
# The REPL installs its renderer here so a long-running command shows life
# instead of a dead terminal for 60s.
PROGRESS = {"emit": None}


def _progress(line, elapsed):
    if PROGRESS["emit"]:
        PROGRESS["emit"]({"type": "tool_progress", "line": line, "elapsed": elapsed})


def _truncate(text):
    limit = LIMITS["tool_output"]
    if len(text) > limit:
        return text[:limit] + f"\n[truncated {len(text) - limit} chars]"
    return text


def _diff(old, new):
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2))
    adds = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    dels = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    body = "\n".join(diff[2:]) if len(diff) > 2 else ""
    return f"(+{adds} -{dels})\n{body}".rstrip()


# ── search ───────────────────────────────────────────────────────────────────

def _list_files(root):
    """Prefer git's own index: it respects .gitignore, so file search agrees with
    content search (rg already honors it) and doesn't drown in build artifacts.
    The os.walk fallback used to truncate at 5000 entries *silently*."""
    if HAS_GIT:
        r = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", root],
                           capture_output=True, encoding="utf-8", errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            return [l for l in r.stdout.splitlines() if l]
    out, truncated = [], False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, f)))
            if len(out) >= 20_000:
                truncated = True
                break
        if truncated:
            break
    if truncated:
        out.append("[…file list truncated at 20000 entries — narrow the path]")
    return out


def _search(inp):
    pattern = inp["pattern"]
    path = inp.get("path", ".")
    glob = inp.get("glob")
    mode = inp.get("mode", "content")

    if mode == "files":
        rx = re.compile(pattern, re.I)
        hits = [f for f in _list_files(path) if rx.search(f)][:200]
        return {"ok": True, "content": "\n".join(hits) or "no matching files"}

    if HAS_RG:
        args = ["rg", "-n", "--no-heading", "--smart-case", "--max-columns", "300",
                "--max-count", "50", "-g", "!.git", "-g", "!node_modules", "-g", "!.sesame"]
        if glob:
            args += ["-g", glob]
        args += ["-e", pattern, path]
        r = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace")
        if r.returncode == 1:
            return {"ok": True, "content": "no matches"}
        if r.returncode not in (0, 1):
            return {"ok": False, "content": r.stderr or f"rg exited {r.returncode}"}
        lines = [l for l in r.stdout.split("\n") if l]
        extra = f"\n…({len(lines) - 200} more matches)" if len(lines) > 200 else ""
        return {"ok": True, "content": _truncate("\n".join(lines[:200]) + extra)}

    rx = re.compile(pattern, re.I)
    file_rx = None
    if glob:
        file_rx = re.compile("^" + re.escape(glob).replace(r"\*", ".*").replace(r"\?", ".") + "$")
    out = []
    for f in _list_files(path):
        if file_rx and not file_rx.match(os.path.basename(f)):
            continue
        try:
            if os.path.getsize(f) > 512 * 1024:
                continue
            text = Path(f).read_text(errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.split("\n"), 1):
            if rx.search(line):
                out.append(f"{f}:{n}: {line[:300]}")
                if len(out) >= 200:
                    return {"ok": True, "content": "\n".join(out)}
    return {"ok": True, "content": "\n".join(out) or "no matches"}


SEARCH = {
    "name": "search",
    "read_only": True,
    "description": (
        "Internal search over the working directory. mode \"content\" (default) greps file "
        "contents for a regex; mode \"files\" matches file paths. Returns \"path:line: text\" matches."),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "regex to search for"},
            "path": {"type": "string", "description": "directory to search (default \".\")"},
            "glob": {"type": "string", "description": "limit content search to files matching this glob, e.g. \"*.md\""},
            "mode": {"type": "string", "enum": ["content", "files"]},
        },
        "required": ["pattern"],
    },
    "execute": _search,
}


# ── read / write / edit / list ───────────────────────────────────────────────

def _read(inp):
    offset = max(1, int(inp.get("offset") or 1))
    limit = max(1, int(inp.get("limit") or 1500))
    lines = Path(inp["path"]).read_text(encoding="utf-8", errors="replace").split("\n")
    view = lines[offset - 1:offset - 1 + limit]
    body = "\n".join(
        f"{offset + i:>5}  {l[:500] + '…' if len(l) > 500 else l}" for i, l in enumerate(view))
    tail = f"\n…({len(lines)} lines total)" if len(lines) > offset - 1 + limit else ""
    # read/search used to bypass the configured output limit entirely — one read
    # of a big file could push ~10k tokens of context in a single call.
    return {"ok": True, "content": _truncate(body + tail) or "[empty file]"}


READ = {
    "name": "read",
    "read_only": True,
    "description": "Read a file. Returns numbered lines. Use offset/limit for large files.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "1-based first line (default 1)"},
            "limit": {"type": "integer", "description": "max lines (default 1500)"},
        },
        "required": ["path"],
    },
    "execute": _read,
}


def _write(inp):
    p = Path(inp["path"])
    old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(inp["content"], encoding="utf-8")
    verb = "overwrote" if old else "wrote"
    return {"ok": True, "content": _truncate(f"{verb} {p} {_diff(old, inp['content'])}")}


WRITE = {
    "name": "write",
    "read_only": False,
    "description": "Write a file (creates parent directories, overwrites if present). Returns a diff.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
    "execute": _write,
}


def _apply_edits(text, edits):
    """Returns (new_text, error). All-or-nothing: a bad edit aborts the batch."""
    for i, e in enumerate(edits, 1):
        old, new = e.get("old_string", ""), e.get("new_string", "")
        if not old:
            return None, f"edit {i}: old_string is empty"
        count = text.count(old)
        if count == 0:
            return None, f"edit {i}: old_string not found: {old[:60]!r}"
        if count > 1 and not e.get("replace_all"):
            return None, (f"edit {i}: old_string appears {count} times; "
                          f"add context or set replace_all")
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text, None


def _edit(inp):
    p = Path(inp["path"])
    if not p.is_file():
        return {"ok": False, "content": f"not a file: {p}"}
    text = p.read_text(encoding="utf-8", errors="replace")

    edits = inp.get("edits")
    if not edits:
        edits = [{"old_string": inp.get("old_string", ""), "new_string": inp.get("new_string", ""),
                  "replace_all": inp.get("replace_all", False)}]
    new_text, err = _apply_edits(text, edits)
    if err:
        return {"ok": False, "content": err + " (no changes written)"}
    p.write_text(new_text, encoding="utf-8")
    n = len(edits)
    return {"ok": True, "content": _truncate(
        f"edited {p} ({n} change{'s' if n > 1 else ''}) {_diff(text, new_text)}")}


EDIT = {
    "name": "edit",
    "read_only": False,
    "description": (
        "Surgical file edit. Either a single old_string→new_string replacement, or a batch via "
        "`edits: [{old_string, new_string, replace_all?}, …]` applied in order, all-or-nothing. "
        "Batch your changes to one file into ONE call — each call is a full round-trip. "
        "Prefer this over rewriting whole files. Returns a diff."),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
            "edits": {
                "type": "array",
                "description": "batch of edits applied in order (alternative to old/new_string)",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path"],
    },
    "execute": _edit,
}


def _list_dir(inp):
    p = Path(inp.get("path", "."))
    if not p.is_dir():
        return {"ok": False, "content": f"not a directory: {p}"}
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    rows = [f"{'d' if e.is_dir() else '-'} {e.name}" for e in entries
            if not e.name.startswith(".") or e.name in (".env", ".gitignore")]
    return {"ok": True, "content": "\n".join(rows) or "[empty]"}


LIST = {
    "name": "list",
    "read_only": True,
    "description": "List a directory (d = dir, - = file). Hidden entries are skipped.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "default \".\""}},
    },
    "execute": _list_dir,
}


# ── bash ─────────────────────────────────────────────────────────────────────

def _kill_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _bash(inp):
    timeout = inp.get("timeout_ms", LIMITS["bash_timeout"] * 1000) / 1000
    out = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            inp["command"], shell=True, stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, text=True,
            encoding="utf-8", errors="replace")
    except Exception as exc:
        out.close()
        return {"ok": False, "content": f"failed to launch: {exc}"}

    start = time.time()
    deadline = start + timeout
    killed = None
    seen = 0
    try:
        while proc.poll() is None:
            if time.time() > deadline:
                killed = f"timed out after {int(timeout)}s"
                break
            time.sleep(0.1)
            # Tail the output so the user can see the command is alive.
            try:
                out.seek(seen)
                chunk = out.read()
                if chunk:
                    seen += len(chunk)
                    tail = [l for l in chunk.splitlines() if l.strip()]
                    if tail:
                        _progress(tail[-1], int(time.time() - start))
                elif int(time.time() - start) > 0:
                    _progress("running…", int(time.time() - start))
            except (OSError, ValueError):
                pass
    except KeyboardInterrupt:
        _kill_group(proc)  # the whole process group dies with the run
        out.close()
        raise
    if killed:
        _kill_group(proc)

    out.seek(0)
    text = _truncate(out.read())
    out.close()
    code = proc.returncode
    header = "" if code == 0 and not killed else f"[exit {code}{'; ' + killed if killed else ''}]\n"
    return {"ok": True, "content": (header + text).rstrip() or "(no output)"}


BASH = {
    "name": "bash",
    "read_only": False,
    "description": ("Run a shell command in the working directory (its own process group; killed "
                    "cleanly on timeout or interrupt). Returns stdout+stderr. Use `... &` for long jobs."),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_ms": {"type": "integer", "description": "default 180000"},
        },
        "required": ["command"],
    },
    "execute": _bash,
}


# ── browse ───────────────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}
    BREAK = {"p", "div", "section", "article", "tr", "blockquote", "pre", "table",
             "ul", "ol", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title = [], ""
        self.skip, self.in_title = 0, False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        elif tag in self.SKIP:
            self.skip += 1
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BREAK or tag == "li":
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def _html_to_text(html):
    p = _TextExtractor()
    p.feed(html)
    lines = [re.sub(r"\s+", " ", l).strip() for l in "".join(p.parts).split("\n")]
    return p.title.strip() or "(untitled)", "\n".join(l for l in lines if l)


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "user-agent": UA,
        "accept": "text/html,application/xhtml+xml,application/json,text/*;q=0.9,*/*;q=0.8",
        "accept-encoding": "gzip, identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read(2_000_000)
        if (res.headers.get("content-encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        charset = res.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace"), res.headers.get("content-type", ""), res.url


def _browse(inp):
    try:
        text, ctype, final_url = _get(inp["url"])
    except urllib.error.HTTPError as exc:
        body = exc.read()[:2000].decode("utf-8", "replace")
        return {"ok": False, "content": f"HTTP {exc.code} from {inp['url']}\n{body}"}
    if "html" in ctype:
        title, page = _html_to_text(text)
        return {"ok": True, "content": f"title: {title}\nurl: {final_url}\n\n{page[:25_000]}"}
    return {"ok": True, "content": f"url: {final_url}\ncontent-type: {ctype}\n\n{text[:25_000]}"}


BROWSE = {
    "name": "browse",
    "read_only": True,
    "description": (
        "Fetch a URL. HTML is converted to readable text; other content is returned raw. "
        "Returns title, final URL, and page text."),
    "input_schema": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "http(s) URL"}},
        "required": ["url"],
    },
    "execute": _browse,
}


# ── websearch (DuckDuckGo HTML endpoint, stdlib parse) ───────────────────────

class _DDG(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results, self.cur = [], None
        self.field, self._ftag, self._fdepth = None, None, 0

    def _flush(self):
        if self.cur and self.cur.get("title"):
            self.results.append(self.cur)
        self.cur = None

    def handle_starttag(self, tag, attrs):
        if self.field:
            if tag == self._ftag:
                self._fdepth += 1
            return
        a = dict(attrs)
        cls = a.get("class", "")
        if tag == "a" and "result__a" in cls:
            self._flush()
            self.cur = {"title": "", "url": a.get("href", ""), "snippet": ""}
            self.field, self._ftag, self._fdepth = "title", tag, 0
        elif "result__snippet" in cls and self.cur is not None:
            self.field, self._ftag, self._fdepth = "snippet", tag, 0

    def handle_endtag(self, tag):
        if self.field and tag == self._ftag:
            if self._fdepth:
                self._fdepth -= 1
            else:
                self.field = None

    def handle_data(self, data):
        if self.cur is not None and self.field:
            self.cur[self.field] += data

    def close(self):
        super().close()
        self._flush()


def _clean_url(href):
    if "uddg=" in href:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        if q.get("uddg"):
            return q["uddg"][0]
    return "https:" + href if href.startswith("//") else href


def _websearch(inp):
    count = inp.get("count", 6)
    data = urllib.parse.urlencode({"q": inp["query"]}).encode()
    req = urllib.request.Request("https://html.duckduckgo.com/html/", data=data, headers={
        "user-agent": UA, "content-type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read(2_000_000)
            if (res.headers.get("content-encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            html = raw.decode("utf-8", "replace")
    except Exception as exc:
        return {"ok": False, "content": f"search error: {exc}"}
    p = _DDG()
    p.feed(html)
    p.close()
    out = []
    for r in p.results[:count]:
        title = re.sub(r"\s+", " ", r["title"]).strip()
        snippet = re.sub(r"\s+", " ", r["snippet"]).strip()
        out.append(f"- {title}\n  {_clean_url(r['url'])}\n  {snippet}")
    return {"ok": True, "content": "\n".join(out) or "no results"}


WEBSEARCH = {
    "name": "websearch",
    "read_only": True,
    "description": "Web search (DuckDuckGo). Returns title, URL, and snippet per result.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "count": {"type": "integer", "description": "max results (default 6)"},
        },
        "required": ["query"],
    },
    "execute": _websearch,
}


TOOLS = [SEARCH, READ, WRITE, EDIT, LIST, BASH, BROWSE, WEBSEARCH]
