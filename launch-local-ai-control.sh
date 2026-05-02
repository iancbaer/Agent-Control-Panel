#!/usr/bin/env bash
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR" || exit 1
exec /usr/bin/python3 "$APP_DIR/local_ai_control.py"
