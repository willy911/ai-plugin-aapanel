#!/bin/bash
# Buat zip plugin untuk Import aaPanel (pola plugin Cloudflare Tunnel:
# zip isi folder plugin saja, TANPA data/ dan bytecode).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(python3 -c "import json; print(json.load(open('$ROOT/ai_chat_agent/info.json'))['versions'])")"
mkdir -p "$ROOT/dist"
rm -f "$ROOT/dist/ai_chat_agent-$VERSION.zip"
cd "$ROOT/ai_chat_agent"
zip -r "$ROOT/dist/ai_chat_agent-$VERSION.zip" . \
    -x "data/*" "__pycache__/*" "*.pyc"
echo "OK: $ROOT/dist/ai_chat_agent-$VERSION.zip"
unzip -l "$ROOT/dist/ai_chat_agent-$VERSION.zip"
