#!/usr/bin/env bash
# stop.sh -- stop the Hy3 model server (frees the ~92 GB of memory it holds).
pkill -f "llama-server.*hy3-295b" 2>/dev/null && echo "Hy3 server stopped." || echo "Hy3 server was not running."
