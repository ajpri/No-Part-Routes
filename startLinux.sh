#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    printf '%s\n' 'Python 3.10 or later is required. Install Python, then run this script again.' >&2
    exit 1
fi
for name in settings apiKeys locations; do
    if [ ! -f "$name.ini" ]; then
        cp "$name.example.ini" "$name.ini"
    fi
done
exec python3 no_part_routes.py "$@"
