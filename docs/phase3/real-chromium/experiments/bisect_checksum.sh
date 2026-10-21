#!/bin/bash
# usage: bisect_checksum.sh <brave_version> <skill_dir> <workroot>   (downloads the Brave Linux zip from GitHub release assets, probes it, deletes it)
set -u
V=$1; SKILL=$2; ROOT=$3; HERE=$(cd "$(dirname "$0")" && pwd)
D=$ROOT/downloads/bisect/$V; rm -rf "$D"; mkdir -p "$D"
curl -sS -L -m 600 -o "$D/b.zip" "https://github.com/brave/brave-browser/releases/download/v$V/brave-browser-$V-linux-amd64.zip" || { echo "download failed"; exit 1; }
unzip -q "$D/b.zip" -d "$D/x" && rm "$D/b.zip"
"$D/x/brave" --version 2>&1 | head -1
PYTHONDONTWRITEBYTECODE=1 python3 "$HERE/probe_checksum.py" "$SKILL" "$ROOT/work/A/reexport2.json" "$ROOT/work/B0/udd" "$D/work" "$D/x/brave"
rm -rf "$D"
