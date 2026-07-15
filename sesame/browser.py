"""sesame agent — browser.py — a REAL browser, not a fetch.

You asked for a browser calling tool. I gave you a urllib GET and filed real
browser control under "not ported" to protect a zero-dependency rule I invented.
That was wrong. This is the old sesame's Playwright browser, restored: a live
Chromium page the agent can navigate, read, click, type into, and screenshot —
so JS-rendered pages, logins, and forms actually work.

Playwright is an optional extra: `pip install playwright && playwright install
chromium`. Without it the tools return an instruction instead of crashing, and
`browse`/`websearch` (stdlib) still work for plain pages.
"""

import queue
import threading
from pathlib import Path

STATE = {"pw": None, "browser": None, "page": None, "headed": False}
INSTALL = ("playwright is not installed — run:\n"
           "  pip install playwright && playwright install chromium\n"
           "(or use the `browse` tool for plain, non-JS pages)")

# ── one thread owns the browser ──────────────────────────────────────────────
# Playwright's sync objects belong to the thread that made them. sesame runs
# tools on a worker thread, and read-only tools in a pool, so each call arrived
# on a different thread: the page was built on one, and the next call, from
# another, failed with "cannot switch to a different thread (which happens to
# have exited)". Every browser call is therefore handed to one long-lived
# thread, which also serialises them: two parallel reads cannot race one page.
_WORK = queue.Queue()
_THREAD = None
_START = threading.Lock()


def _pump():
    while True:
        fn, arg, box, done = _WORK.get()
        try:
            box.append(("ok", fn(arg)))
        except BaseException as exc:               # noqa: BLE001 - reraised to the caller
            box.append(("error", exc))
        finally:
            done.set()


def _on_browser_thread(fn, arg, timeout=300):
    global _THREAD
    with _START:
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(target=_pump, name="sesame-browser", daemon=True)
            _THREAD.start()
    box, done = [], threading.Event()
    _WORK.put((fn, arg, box, done))
    if not done.wait(timeout):
        raise TimeoutError(f"the browser did not answer within {timeout}s")
    kind, value = box[0]
    if kind == "error":
        raise value
    return value


def available():
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def _page():
    if STATE["page"] is not None:
        return STATE["page"]
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=not STATE["headed"])
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    STATE.update(pw=pw, browser=browser, page=page)
    return page


def _close(_=None):
    try:
        if STATE["browser"]:
            STATE["browser"].close()
        if STATE["pw"]:
            STATE["pw"].stop()
    except Exception:
        pass
    STATE.update(pw=None, browser=None, page=None)


def shutdown():
    """Closed on the thread that opened it, or Playwright refuses."""
    if STATE["pw"] is None and STATE["browser"] is None:
        return
    try:
        _on_browser_thread(_close, None, timeout=20)
    except Exception:
        STATE.update(pw=None, browser=None, page=None)


# errors that mean the page is gone, not that the call was wrong
_DEAD = ("cannot switch to a different thread", "target closed", "target page",
         "browser has been closed", "browser closed", "connection closed", "page closed")


def _guard(fn):
    def wrapped(inp):
        if not available():
            return {"ok": False, "content": INSTALL}
        try:
            return _on_browser_thread(fn, inp)
        except Exception as exc:  # a dead page must not kill the run
            detail = f"{type(exc).__name__}: {exc}"
            if any(d in str(exc).lower() for d in _DEAD):
                # a corpse, not a mistake: drop it so the next call opens a fresh
                # page instead of failing forever. A bad selector is NOT this, and
                # must not cost you the browser you are logged into.
                shutdown()
                detail += "  (browser restarted, run the call again)"
            return {"ok": False, "content": f"browser error: {detail}"}
    return wrapped


@_guard
def _navigate(inp):
    url = inp["url"]
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    page = _page()
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    return {"ok": True, "content": f"navigated to {page.url}\ntitle: {page.title()}"}


@_guard
def _read(inp):
    page = _page()
    limit = int(inp.get("max_chars") or 8000)
    text = page.evaluate("() => document.body ? document.body.innerText : ''")
    clipped = text[:limit] + ("\n[truncated]" if len(text) > limit else "")
    return {"ok": True, "content": f"url: {page.url}\n\n{clipped}" if text else "[empty page]"}


@_guard
def _click(inp):
    page = _page()
    page.click(inp["selector"], timeout=15_000)
    page.wait_for_load_state("domcontentloaded", timeout=15_000)
    return {"ok": True, "content": f"clicked {inp['selector']} · now at {page.url}"}


@_guard
def _type(inp):
    page = _page()
    page.fill(inp["selector"], inp["text"], timeout=15_000)
    if inp.get("submit"):
        page.press(inp["selector"], "Enter")
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    return {"ok": True, "content": f"typed into {inp['selector']}"
                                   + (f" and submitted · now at {page.url}" if inp.get("submit") else "")}


@_guard
def _screenshot(inp):
    page = _page()
    p = Path(inp.get("path") or "screenshot.png")
    page.screenshot(path=str(p), full_page=bool(inp.get("full_page", True)))
    return {"ok": True, "content": f"saved screenshot to {p.resolve()}"}


NAVIGATE = {
    "name": "browser_navigate", "read_only": True,
    "description": ("Open a URL in a REAL browser (Chromium). Use this for JS-heavy, "
                    "interactive, or logged-in pages where the plain `browse` fetch is not enough."),
    "input_schema": {"type": "object", "properties": {"url": {"type": "string"}},
                     "required": ["url"]},
    "execute": _navigate,
}
READ = {
    "name": "browser_read", "read_only": True,
    "description": "Read the visible text of the current browser page (after JS has run).",
    "input_schema": {"type": "object",
                     "properties": {"max_chars": {"type": "integer", "description": "default 8000"}}},
    "execute": _read,
}
CLICK = {
    "name": "browser_click", "read_only": False,
    "description": "Click an element in the browser page by CSS selector.",
    "input_schema": {"type": "object", "properties": {"selector": {"type": "string"}},
                     "required": ["selector"]},
    "execute": _click,
}
TYPE = {
    "name": "browser_type", "read_only": False,
    "description": "Type text into an element by CSS selector; set submit to press Enter.",
    "input_schema": {"type": "object",
                     "properties": {"selector": {"type": "string"}, "text": {"type": "string"},
                                    "submit": {"type": "boolean"}},
                     "required": ["selector", "text"]},
    "execute": _type,
}
SCREENSHOT = {
    "name": "browser_screenshot", "read_only": False,
    "description": "Save a full-page screenshot of the current browser page.",
    "input_schema": {"type": "object",
                     "properties": {"path": {"type": "string", "description": "default screenshot.png"},
                                    "full_page": {"type": "boolean"}}},
    "execute": _screenshot,
}

TOOLS = [NAVIGATE, READ, CLICK, TYPE, SCREENSHOT]
