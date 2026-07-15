"""sesame agent — doctor.py — setup wizard and health check."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import models
import project
import providers

HERE = Path(__file__).resolve().parent


def _config():
    """Same layering the app uses: install defaults → ~/.sesame → ./.sesame → env."""
    install = {}
    f = HERE / "sesame.config.json"
    if f.is_file():
        try:
            install = json.loads(f.read_text())
        except json.JSONDecodeError:
            pass
    return project.load_config(install)

OK, WARN, BAD = "\x1b[32m✓\x1b[0m", "\x1b[33m!\x1b[0m", "\x1b[31m✗\x1b[0m"
DIM, BOLD, CYAN = "\x1b[2m", "\x1b[1m", "\x1b[36m"
END = "\x1b[0m"


def _ask(prompt, default=""):
    try:
        got = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    return got or default


# ── first-run setup ──────────────────────────────────────────────────────────

def needs_setup(cfg):
    return not cfg.get("apiKey")


def setup():
    """Pick a provider (hosted, local, or your own endpoint), take a key, test it."""
    print(f"\n{BOLD}Welcome to sesame{END} — let's get you set up.\n")
    names = providers.names()
    for i, n in enumerate(names, 1):
        url, wire, model = providers.PRESETS[n]
        tag = f"{DIM}· local, no key{END}" if n in providers.LOCAL else ""
        print(f"  {CYAN}{i:>2}{END}  {n:<17} {DIM}{model:<40}{END} {tag}")
    print(f"  {CYAN}{len(names) + 1:>2}{END}  {'custom':<17} {DIM}any OpenAI- or "
          f"Anthropic-compatible endpoint{END}")
    print()
    choice = _ask("Which provider? (number or name)", "deepseek")

    if choice == str(len(names) + 1) or choice.lower() == "custom":
        url = _ask("Base URL (e.g. http://localhost:8000/v1)")
        if not url:
            return False
        wire = _ask("API format — openai or anthropic", "openai")
        model = _ask("Model id")
        key = _ask("API key (blank for local)", "")
        name = "custom"
    else:
        name = (names[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(names)
                else choice)
        preset = providers.preset(name)
        if not preset:
            print(f"{BAD} unknown provider: {name}")
            return False
        url, wire, model = preset
        if providers.is_local(name):
            print(f"{DIM}local provider — no API key needed{END}")
            key = ""
        else:
            key = _ask(f"API key for {name}")
            if not key:
                print(f"{BAD} a key is required for {name}")
                return False
        model = _ask("Model", model)

    cfg = {"provider": name, "baseUrl": url, "wire": wire, "model": model, "apiKey": key}
    print(f"\n{DIM}testing…{END}")
    err = _probe(cfg)
    if err:
        print(f"{BAD} {err}")
        if providers.is_local(cfg["baseUrl"]):
            print(f"{DIM}  is the server running? ollama: `ollama serve` · "
                  f"LM Studio: start the local server{END}")
        if _ask("Save anyway? (y/N)", "n").lower() != "y":
            return False
    else:
        print(f"{OK} connected to {name}")

    path = project.save_config(cfg, scope="home")
    print(f"{OK} saved to {path}\n")
    return True


def _probe(cfg):
    """One tiny request. Returns an error string, or None if it worked."""
    from shell import _stream, APIError
    model = cfg.get("model") or "deepseek-v4-flash"
    api = {"base_url": cfg.get("baseUrl") or providers.PRESETS["deepseek"][0],
           "api_key": cfg.get("apiKey", ""), "model": model,
           "max_tokens": 64, "wire": cfg.get("wire", "anthropic"),
           "thinking": models.spec(model)["thinking"], "interleaved": True}
    try:
        _stream(transcript=[{"role": "user", "content": "hi"}], system="", tools=None,
                api=api, budget={"thinking_tokens": 128, "effort": "low"}, emit=lambda e: None)
        return None
    except APIError as exc:
        if exc.status in (401, 403):
            return "the API key was rejected"
        return f"API {exc.status}: {str(exc.detail)[:120]}"
    except Exception as exc:
        return f"could not reach {cfg['baseUrl']}: {exc}"


# ── doctor ───────────────────────────────────────────────────────────────────

def doctor(fix=False):
    print(f"\n{BOLD}sesame doctor{END}\n")
    problems = []

    # python
    v = sys.version_info
    if v >= (3, 10):
        print(f"{OK} python {v.major}.{v.minor}")
    else:
        print(f"{BAD} python {v.major}.{v.minor} — 3.10+ required")
        problems.append("upgrade python to 3.10 or newer")

    # required deps
    for mod, pkg in (("prompt_toolkit", "prompt_toolkit"),):
        try:
            __import__(mod)
            print(f"{OK} {mod}")
        except ImportError:
            print(f"{BAD} {mod} is missing")
            if fix and _pip(pkg):
                print(f"  {OK} installed {pkg}")
            else:
                problems.append(f"pip install {pkg}")

    # optional deps
    if shutil.which("rg"):
        print(f"{OK} ripgrep {DIM}(fast search){END}")
    else:
        print(f"{WARN} ripgrep not found {DIM}— search still works, just slower{END}")
    if shutil.which("git"):
        print(f"{OK} git")
    else:
        print(f"{WARN} git not found {DIM}— no branch info, no gitignore-aware search{END}")
    try:
        import playwright  # noqa: F401
        print(f"{OK} playwright {DIM}(browser tools){END}")
    except ImportError:
        print(f"{WARN} playwright not installed {DIM}— browser_* tools disabled{END}")
        if fix and _ask("  install playwright + chromium? (y/N)", "n").lower() == "y":
            if _pip("playwright"):
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
                print(f"  {OK} browser ready")

    # config
    cfg = _config()
    if cfg.get("apiKey"):
        masked = cfg["apiKey"][:7] + "…"
        print(f"{OK} api key {DIM}({masked}){END}")
    elif providers.is_local(cfg.get("baseUrl", "")):
        print(f"{OK} local endpoint {DIM}(no key needed){END}")
    else:
        print(f"{BAD} no API key")
        if fix:
            setup()
            cfg = _config()
        else:
            problems.append("run: ./run.sh setup")

    model = cfg.get("model") or "deepseek-v4-flash"
    if model in models.MODELS:
        s = models.spec(model)
        print(f"{OK} model {model} {DIM}({s['window'] // 1000}k ctx · "
              f"${s['in']}/${s['out']} per Mtok){END}")
    elif cfg.get("contextWindow"):
        # a model you serve yourself: the window is not a guess, you (or the
        # server) told us what it is
        print(f"{OK} model {model} {DIM}({int(cfg['contextWindow']) // 1000}k ctx · "
              f"your own endpoint, no pricing){END}")
    else:
        print(f"{WARN} model {model} {DIM}— not in the registry: cost and context "
              f"window are guesses{END}")

    # profiles: a profile whose local server is not running is the failure you
    # actually hit, and it looks like a hang rather than a stopped server
    profs = project.load_profiles()
    if profs:
        for name, prof in sorted(profs.items()):
            url = prof.get("baseUrl", "")
            if not providers.is_local(url):
                print(f"{OK} profile {name} {DIM}({prof.get('model', '?')}){END}")
                continue
            if _probe(prof):
                print(f"{WARN} profile {name} {DIM}({prof.get('model', '?')}) — "
                      f"{url} is not answering; start its server{END}")
            else:
                print(f"{OK} profile {name} {DIM}({prof.get('model', '?')} · "
                      f"{url}){END}")

    # connectivity
    if cfg.get("apiKey") or providers.is_local(cfg.get("baseUrl", "")):
        err = _probe(cfg)
        if err:
            print(f"{BAD} {cfg.get('baseUrl') or '(default endpoint)'} — {err}")
            problems.append("check your key / base URL: ./run.sh setup")
        else:
            print(f"{OK} {cfg.get('baseUrl') or 'default endpoint'} responds")

    # workspace
    d = Path(".sesame")
    try:
        d.mkdir(exist_ok=True)
        (d / ".probe").write_text("x")
        (d / ".probe").unlink()
        print(f"{OK} .sesame is writable {DIM}(sessions, undo, permissions){END}")
    except OSError as exc:
        print(f"{BAD} cannot write .sesame — {exc}")
        problems.append("fix permissions on this directory")

    if Path("AGENTS.md").is_file() or Path("CLAUDE.md").is_file():
        print(f"{OK} project instructions loaded")
    else:
        print(f"{WARN} no AGENTS.md {DIM}— the agent knows nothing about this repo "
              f"(/init to create one){END}")

    print()
    if problems:
        print(f"{BOLD}To fix:{END}")
        for p in problems:
            print(f"  · {p}")
        print(f"\n{DIM}or run: ./run.sh doctor --fix{END}\n")
        return 1
    print(f"{OK} {BOLD}everything checks out{END}\n")
    return 0


def _pip(pkg):
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", pkg])
    return r.returncode == 0
