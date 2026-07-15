#!/usr/bin/env bash
# ============================================================================
# setup.sh  --  one-time setup for sesame-hy3 on an Apple Silicon Mac (128GB).
#
# Does everything needed to run, and is safe to run again (each step is skipped
# if already done):
#   1. checks the tools it needs (Xcode CLT, cmake, python3, the hf CLI)
#   2. builds the hy_v3-patched llama.cpp   (~10-20 min, once)
#   3. downloads the Hy3 model, 85 GB       (once; reuses one you already have)
#   4. creates a Python venv for the agent and installs its one dependency
#
# Run it directly, or just double-click "Start Sesame Hy3.command" which calls it.
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$HERE/engine/llama.cpp-hyv3"
SERVER="$ENGINE/build/bin/llama-server"
MODELS="$HERE/models"
MODEL="$MODELS/Hy3-IQ1_M-mtp.gguf"
VENV="$HERE/sesame/.venv"

HF_REPO="AngelSlim/Hy3-GGUF"
HF_FILE="Hy3-IQ1_M-mtp.gguf"
MODEL_BYTES=91756066624

c()   { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok()  { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }
warn(){ printf '\033[33m  ! %s\033[0m\n' "$*"; }
die() { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

c "sesame-hy3 setup"
echo

# ── 0. platform ──────────────────────────────────────────────────────────────
[ "$(uname -s)" = "Darwin" ] || die "this is for macOS (Apple Silicon)."
[ "$(uname -m)" = "arm64" ]  || die "this needs Apple Silicon (arm64)."
RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
c "1. system"
ok "macOS on Apple Silicon, ${RAM_GB} GB RAM"
[ "$RAM_GB" -ge 120 ] || warn "Hy3 at 256k wants ~118 GB usable. ${RAM_GB} GB is tight; you may be capped to a smaller context."

# ── 1. tools ─────────────────────────────────────────────────────────────────
c "2. tools"
if ! xcode-select -p >/dev/null 2>&1; then
  warn "Xcode command line tools are needed. Installing (a dialog will open)…"
  xcode-select --install || true
  die "finish the Xcode tools install, then run setup again."
fi
ok "Xcode command line tools"

need_brew=0
command -v cmake >/dev/null 2>&1 || need_brew=1
if [ "$need_brew" = "1" ]; then
  if command -v brew >/dev/null 2>&1; then
    warn "installing cmake via Homebrew…"; brew install cmake
  else
    die "cmake not found and Homebrew not installed. Install cmake, or install Homebrew from https://brew.sh"
  fi
fi
ok "cmake $(cmake --version | head -1 | awk '{print $3}')"

command -v python3 >/dev/null 2>&1 || die "python3 not found."
python3 - <<'PY' || die "Python 3.10+ required."
import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)
PY
ok "python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

# the hf CLI (for the model download). Install into the agent venv later if missing.
HF=""
for cand in hf huggingface-cli; do command -v "$cand" >/dev/null 2>&1 && { HF="$cand"; break; }; done

# ── 2. build the engine ──────────────────────────────────────────────────────
c "3. inference engine (hy_v3-patched llama.cpp)"
if [ -x "$SERVER" ]; then
  ok "already built ($SERVER)"
elif [ -n "${HY3_ENGINE:-}" ] && [ -x "$HY3_ENGINE" ]; then
  mkdir -p "$(dirname "$ENGINE")"
  ln -sfn "$(cd "$(dirname "$HY3_ENGINE")/../.." && pwd)" "$ENGINE"
  ok "reusing your existing engine via HY3_ENGINE"
else
  warn "building llama.cpp with the hy_v3 patches. This takes 10-20 minutes, once."
  ( cd "$HERE/engine" && JOBS="$(sysctl -n hw.ncpu)" GGML_NATIVE=1 CUDA=0 \
      bash setup_hyv3_llama.sh "$ENGINE" )
  [ -x "$SERVER" ] || die "engine build did not produce $SERVER"
  ok "engine built"
fi

# ── 3. download the model ────────────────────────────────────────────────────
c "4. model (Hy3-295B, IQ1_M, 85 GB)"
mkdir -p "$MODELS"
if [ -f "$MODEL" ] && [ "$(stat -f%z "$MODEL")" = "$MODEL_BYTES" ]; then
  ok "already present ($MODEL)"
elif [ -n "${HY3_MODEL:-}" ] && [ -f "$HY3_MODEL" ]; then
  ln -sfn "$HY3_MODEL" "$MODEL"
  ok "reusing your existing model via HY3_MODEL"
else
  # reuse a copy already on the machine before downloading 85 GB again
  FOUND=$(find "$HOME/Documents" -maxdepth 4 -name "$HF_FILE" -size +80G 2>/dev/null | head -1 || true)
  if [ -n "$FOUND" ]; then
    warn "found an existing copy at $FOUND — linking it instead of downloading"
    ln -sfn "$FOUND" "$MODEL"; ok "linked"
  else
    [ -n "$HF" ] || {
      warn "installing the Hugging Face CLI to download the model…"
      python3 -m pip install --quiet --user huggingface_hub || die "could not install huggingface_hub"
      HF="$(python3 -c 'import huggingface_hub,os;print(os.path.dirname(huggingface_hub.__file__))' >/dev/null 2>&1; echo hf)"
      command -v hf >/dev/null 2>&1 || HF="python3 -m huggingface_hub.commands.huggingface_cli"
    }
    warn "downloading $HF_FILE (85 GB) from $HF_REPO — this takes a while."
    $HF download "$HF_REPO" "$HF_FILE" --local-dir "$MODELS" || die "model download failed"
    [ -f "$MODEL" ] || die "download did not produce $MODEL"
    ok "model downloaded"
  fi
fi

# ── 4. the agent's python env ────────────────────────────────────────────────
c "5. agent"
if [ ! -d "$VENV" ]; then
  python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/python" -m pip install --quiet prompt_toolkit || die "could not install prompt_toolkit"
ok "agent ready"

echo
c "setup complete."
echo "  Start it with:  ./start.sh   (or double-click \"Start Sesame Hy3.command\")"
