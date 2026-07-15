#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_hyv3_llama.sh — one-click: clone llama.cpp, pin base, apply the two HY_V3
#                       patches, build. Produces a llama.cpp with hy_v3 support
#                       (+ MTP speculative decoding, tool/thinking parser).
#
# Usage:
#   bash setup_hyv3_llama.sh [TARGET_DIR]
#
#   TARGET_DIR   where to clone llama.cpp   (default: ./llama.cpp-hyv3)
#
# Env knobs:
#   CUDA=1|0        build the CUDA backend        (default: auto-detect nvcc)
#   SERVER=1|0      build llama-server            (default: 1)
#   OPENSSL=1|0     server HTTPS model download   (default: 0 — off for portability)
#   JOBS=N          parallel build jobs           (default: nproc)
#   GGML_NATIVE=1|0 -march=native on/off          (default: auto-detected)
#   LLAMA_REPO=url  llama.cpp git remote          (default: github ggml-org)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# The exact llama.cpp commit these patches were generated against and verified on.
# Do NOT change casually: the patches are line-anchored to this tree.
LLAMA_COMMIT="19bba67c1f4db723c60a0d421aa0788bf4ddc699"
LLAMA_REPO="${LLAMA_REPO:-https://github.com/ggml-org/llama.cpp.git}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_DIR="$HERE/patches"
TARGET="${1:-$PWD/llama.cpp-hyv3}"
JOBS="${JOBS:-$(nproc)}"

# CUDA: default on if nvcc present, else CPU build.
if [ -z "${CUDA:-}" ]; then
    command -v nvcc >/dev/null 2>&1 && CUDA=1 || CUDA=0
fi
SERVER="${SERVER:-1}"

say() { printf '\033[1;36m[hyv3-setup]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[hyv3-setup] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

for p in "$PATCH_DIR/01-hyv3-arch.patch" "$PATCH_DIR/02-hyv3-mtp-tools.patch"; do
    [ -f "$p" ] || die "patch not found: $p"
done
command -v git   >/dev/null || die "git not found"
command -v cmake >/dev/null || die "cmake not found"

# ── 1) clone + pin ───────────────────────────────────────────────────────────
if [ -d "$TARGET/.git" ]; then
    say "reuse existing clone at $TARGET"
else
    say "cloning llama.cpp -> $TARGET"
    git clone "$LLAMA_REPO" "$TARGET"
fi
cd "$TARGET"

say "checkout pinned base $LLAMA_COMMIT"
git fetch --depth=1 origin "$LLAMA_COMMIT" 2>/dev/null || git fetch origin 2>/dev/null || true
git checkout -q "$LLAMA_COMMIT" || die "cannot checkout $LLAMA_COMMIT (shallow clone? try a full 'git clone')"

# Refuse to patch a dirty tree (avoid double-apply / conflicts).
if ! git diff --quiet || ! git diff --cached --quiet; then
    die "working tree is dirty. Reset it first:  git -C $TARGET reset --hard $LLAMA_COMMIT && git -C $TARGET clean -fd"
fi

# ── 2) apply the two patches in order ────────────────────────────────────────
say "applying 01-hyv3-arch.patch (base arch)"
git apply --check "$PATCH_DIR/01-hyv3-arch.patch" || die "01 does not apply — is the base commit correct?"
git apply         "$PATCH_DIR/01-hyv3-arch.patch"

say "applying 02-hyv3-mtp-tools.patch (MTP + parser)"
git apply --check "$PATCH_DIR/02-hyv3-mtp-tools.patch" || die "02 does not apply on top of 01"
git apply         "$PATCH_DIR/02-hyv3-mtp-tools.patch"

say "patches applied cleanly."

# ── 3) build ─────────────────────────────────────────────────────────────────
# Official docs/build.md commands; we add -DLLAMA_BUILD_SERVER=ON (server is needed
# for --spec-type MTP).
CMAKE_ARGS=( -B build -DCMAKE_BUILD_TYPE=Release )
if [ "$CUDA" = "1" ]; then CMAKE_ARGS+=( -DGGML_CUDA=ON ); say "backend: CUDA";
else                                                       say "backend: CPU-only"; fi
[ "$SERVER" = "1" ] && CMAKE_ARGS+=( -DLLAMA_BUILD_SERVER=ON )

# OpenSSL: off by default (only needed for the server's HTTPS model download; also
# fails to link against an old system OpenSSL). Set OPENSSL=1 to enable.
[ "${OPENSSL:-0}" = "1" ] || { CMAKE_ARGS+=( -DLLAMA_OPENSSL=OFF ); say "OpenSSL: OFF (set OPENSSL=1 to enable HTTPS download)"; }

# GGML_NATIVE: auto-detected. -march=native can emit AVX512-BF16 that an old
# assembler rejects; we probe for that and disable native only when needed.
# Override with GGML_NATIVE=1/0.
PROBE_CC="${CC:-$(command -v gcc || command -v cc || echo cc)}"

detect_native() {
    # returns 0 = keep native, 1 = disable native (assembler lacks AVX512-BF16)
    local src=/tmp/hyv3_bf16_probe.$$.s out=/tmp/hyv3_bf16_probe.$$.o err=/tmp/hyv3_bf16_probe.$$.err
    printf '.text\nvdpbf16ps %%zmm4, %%zmm3, %%zmm1\n' > "$src"
    local rc=0
    if "$PROBE_CC" -c "$src" -o "$out" 2>"$err"; then
        rc=0
    elif grep -q "no such instruction\|bad register\|Error:" "$err"; then
        rc=1
    fi
    rm -f "$src" "$out" "$err"
    return $rc
}

if [ -n "${GGML_NATIVE:-}" ]; then
    # explicit override
    if [ "$GGML_NATIVE" = "0" ]; then CMAKE_ARGS+=( -DGGML_NATIVE=OFF ); say "GGML_NATIVE=OFF (forced)"; else say "GGML_NATIVE=ON (forced)"; fi
elif detect_native; then
    say "GGML_NATIVE: auto → ON (toolchain builds native path)"
else
    CMAKE_ARGS+=( -DGGML_NATIVE=OFF )
    say "GGML_NATIVE: auto → OFF (assembler can't encode AVX512-BF16; portable CPU build)"
fi

say "cmake configure"
cmake "${CMAKE_ARGS[@]}"

# Build only the targets HY_V3 needs (drop the --target list for a full build).
TARGETS=( llama llama-quantize llama-imatrix )
[ "$SERVER" = "1" ] && TARGETS+=( llama-server llama-cli )
say "cmake build --config Release -j $JOBS --target ${TARGETS[*]}"
cmake --build build --config Release -j "$JOBS" --target "${TARGETS[@]}"

say "DONE. Binaries in: $TARGET/build/bin/"
cat <<EOF

Serve (see README.md for conversion, quantization, and multi-GPU tips)
──────────────────────────────────────────────────────────────────────
  plain (no speculative decoding):
    $TARGET/build/bin/llama-server -m /path/to/Hy3.gguf \\
        -ctk q8_0 -ctv q8_0 -fa on -c 65536

  with MTP self-speculative decoding (needs an MTP gguf):
    $TARGET/build/bin/llama-server -m /path/to/Hy3-mtp.gguf \\
        --spec-type draft-mtp --spec-draft-n-max 3 --spec-draft-n-min 1 \\
        -ctk q8_0 -ctv q8_0 -ctkd q8_0 -ctvd q8_0 \\
        -fa on -c 65536
EOF
