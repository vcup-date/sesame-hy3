#!/usr/bin/env bash
# sesame — launcher. First run installs everything and sets up your model.
#
#   ./run.sh              start sesame (installs + sets up on first run)
#   ./run.sh web          the browser interface on http://127.0.0.1:9981
#   ./run.sh doctor       check the install, config, and connection
#   ./run.sh doctor --fix fix what it can
#   ./run.sh setup        change provider / API key / model
#   ./run.sh --help       all flags
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
STAMP="$VENV/.deps-ok"

dim()  { printf '\033[2m%s\033[0m\n' "$1"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$1"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 not found — install Python 3.10+"
python3 - <<'EOF' || die "Python 3.10+ required"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF

# ── first run: private venv so we never touch your system python ─────────────
if [ ! -d "$VENV" ]; then
  dim "first run — setting up sesame in $VENV"
  python3 -m venv --system-site-packages "$VENV" || die "could not create a virtualenv"
fi
PY="$VENV/bin/python"

if [ ! -f "$STAMP" ]; then
  dim "installing dependencies…"
  "$PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  "$PY" -m pip install --quiet prompt_toolkit rich || die "could not install dependencies"
  touch "$STAMP"
  ok "dependencies installed"
fi

export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"

# ── subcommands ──────────────────────────────────────────────────────────────
case "${1:-}" in
  doctor|setup)
    exec "$PY" "$HERE/main.py" "$@"
    ;;
  web)
    shift
    exec "$PY" "$HERE/web/server.py" "$@"
    ;;
esac

# ── first run: nothing to talk to yet → walk through setup ───────────────────
# validate(), not "is there an apiKey": a local model (ollama, lmstudio, your own
# llama-server) needs no key, and demanding one sent it to setup on every launch.
if ! "$PY" - <<'EOF' >/dev/null 2>&1
import config
raise SystemExit(0 if config.Config().validate() is None else 1)
EOF
then
  "$PY" "$HERE/main.py" setup || die "setup did not complete"
fi

exec "$PY" "$HERE/main.py" "$@"
