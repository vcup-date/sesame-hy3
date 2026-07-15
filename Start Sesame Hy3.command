#!/usr/bin/env bash
# Double-click this in Finder to set up (first time) and start Sesame Hy3.
cd "$(dirname "${BASH_SOURCE[0]}")"
clear
echo "Starting Sesame Hy3 — the agent on a local 295B model."
echo
exec ./start.sh
