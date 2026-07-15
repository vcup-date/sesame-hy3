#!/usr/bin/env bash
# ============================================================================
# start.sh  --  run Hy3-295B locally at 256k context, and hand it to sesame.
#
#   ./start.sh            256k context, fastest decode
#   ./start.sh stop       stop the model server
#   CTX=131072 ./start.sh a smaller window (no sudo needed at/below ~192k)
#
# Runs setup.sh first if anything is missing, then:
#   1. raises the macOS GPU memory ceiling so the 256k KV cache fits (one sudo
#      prompt; temporary, resets on reboot). Falls back to the largest window
#      that fits if you decline.
#   2. starts the hy_v3 llama.cpp server at 256k, q4 KV, MTP off (the benchmarked
#      fastest config: ~31 tok/s, unchanged by context length).
#   3. launches sesame pointed at it, under a profile, without touching any other
#      setup you have.
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$HERE/engine/llama.cpp-hyv3/build/bin/llama-server"
MODEL="$HERE/models/Hy3-IQ1_M-mtp.gguf"
AGENT="$HERE/sesame"
PORT=9403
MODEL_ID="hy3-295b"
PROFILE="local-hy3"

CTX="${CTX:-262144}"          # 256k, the model's full native window
KVQ="${KVQ:-q4_0}"            # q4 KV: half the memory of q8, same speed
UBATCH="${UBATCH:-512}"
MTP="${MTP:-0}"               # off: slower on this MoE even at high draft acceptance
WEIGHTS_GIB=85.5
OS_RESERVE_MIB=8192
CEIL_CAP_MIB=122880

if [ "${1:-}" = "stop" ]; then
  pkill -f "llama-server.*${MODEL_ID}" 2>/dev/null && echo "[hy3] server stopped" || echo "[hy3] not running"
  exit 0
fi

# ── make sure everything is built and downloaded ─────────────────────────────
if [ ! -x "$SERVER" ] || [ ! -e "$MODEL" ] || [ ! -d "$AGENT/.venv" ]; then
  echo "[hy3] first run — setting up (build + model download). This is a one-time step."
  bash "$HERE/setup.sh"
fi

# ── GPU memory ceiling: 256k needs ~112 GiB, default macOS budget is ~107.5 ──
need_mib_for() { python3 -c "print(int(($WEIGHTS_GIB + 93312*$1/1024**3 + 3.7)*1024 + 0.5))"; }
current_limit_mib() {
  local v; v=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)
  [ "${v:-0}" -gt 0 ] 2>/dev/null && echo "$v" || echo 110100   # 0 => ~107.5 GiB default
}
max_ctx_for() {
  python3 -c "
avail=$1/1024 - $WEIGHTS_GIB - 3.7
t=int(avail*1024**3/93312)
print(min(262144, max(32768, (t//16384)*16384)))"
}

NEED=$(need_mib_for "$CTX"); HAVE=$(current_limit_mib)
echo "[hy3] ${CTX} context needs ~${NEED} MiB GPU memory; budget is ${HAVE} MiB"
if [ "$NEED" -gt "$HAVE" ]; then
  TARGET=$(( NEED + 1024 )); [ "$TARGET" -gt "$CEIL_CAP_MIB" ] && TARGET=$CEIL_CAP_MIB
  TOTAL=$(( $(sysctl -n hw.memsize) / 1048576 ))
  [ $(( TARGET + OS_RESERVE_MIB )) -gt "$TOTAL" ] && TARGET=$(( TOTAL - OS_RESERVE_MIB ))
  if [ -t 0 ]; then
    echo "[hy3] raising the GPU memory ceiling to ${TARGET} MiB (temporary; resets on reboot):"
    sudo sysctl iogpu.wired_limit_mb="$TARGET" >/dev/null 2>&1 && HAVE=$(current_limit_mib) \
      || echo "[hy3] ceiling not raised (declined)."
  else
    echo "[hy3] no terminal for the password prompt. To use ${CTX}: sudo sysctl iogpu.wired_limit_mb=${TARGET}"
  fi
  if [ "$NEED" -gt "$HAVE" ]; then
    CTX=$(max_ctx_for "$HAVE")
    echo "[hy3] falling back to the largest window that fits now: ${CTX}"
    echo "[hy3]   for the full 256k, run:  sudo sysctl iogpu.wired_limit_mb=${CEIL_CAP_MIB}   then rerun"
  fi
fi

# ── start the model server if it is not already answering ────────────────────
if ! curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "[hy3] starting Hy3 server (ctx=$CTX, KV=$KVQ, MTP=$MTP) on :$PORT …"
  echo "[hy3] first load reads the 85 GB model from disk; give it a minute."
  SPEC=()
  [ "$MTP" = "1" ] && SPEC=(--spec-type draft-mtp --spec-draft-n-max 3 --spec-draft-n-min 1 -ctkd "$KVQ" -ctvd "$KVQ")
  "$SERVER" -m "$MODEL" -a "$MODEL_ID" \
    ${SPEC[@]+"${SPEC[@]}"} \
    -c "$CTX" --parallel 1 -ngl 99 -fa on -ctk "$KVQ" -ctv "$KVQ" -b "$UBATCH" -ub "$UBATCH" \
    --jinja --host 127.0.0.1 --port "$PORT" > /tmp/sesame-hy3.log 2>&1 &
  PID=$!
  for i in $(seq 1 900); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    kill -0 "$PID" 2>/dev/null || { echo "[hy3] server failed to start:"; tail -25 /tmp/sesame-hy3.log; exit 1; }
    sleep 2
  done
fi
echo "[hy3] server ready on :$PORT"

# ── register the profile (visible in /model), without changing any other setup ─
PYTHONPATH="$AGENT" "$AGENT/.venv/bin/python" - "$PORT" "$PROFILE" "$MODEL_ID" "$CTX" <<'EOF'
import sys, models, project
port, name, fallback_id, fallback_window = sys.argv[1:5]
url = f"http://127.0.0.1:{port}/v1"
served = (models.fetch(url, "", "openai", timeout=6) or [fallback_id])[0]
window = models.window_of(url, "", "openai", served, timeout=6) or int(fallback_window)
profs = project.load_profiles()
profs[name] = {"provider": "custom", "baseUrl": url, "wire": "openai", "model": served,
               "apiKey": "", "contextWindow": window, "thinking": "none", "effort": "low"}
project.save_profiles(profs)
print(f"[hy3] model={served}  context={window}")
EOF

# ── launch the agent, under that profile, for this session ───────────────────
echo "[hy3] starting sesame…"
echo
export SESAME_PROFILE="$PROFILE"
cd "$AGENT"
exec ./run.sh "$@"
