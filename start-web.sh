#!/usr/bin/env bash
# start-web.sh -- same as start.sh, but opens the browser interface instead of
# the terminal one. http://127.0.0.1:9981
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/start.sh" web
