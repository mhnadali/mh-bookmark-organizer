#!/bin/bash
# Experiment B1: write the synthetic library into a Chromium-written profile with `mhbm install-chrome`, launch real Chromium on it,
# compare chrome.bookmarks.getTree() with what we wrote, then let Chromium rewrite the file and verify that again.
# usage: exp_b_check.sh <workdir> <skill_dir> <library_home> [chromium_bin]
set -u
W=$1; SKILL=$2; HOME_LIB=$3; BIN=${4:-}
HARNESS=$(cd "$(dirname "$0")" && pwd)
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$SKILL/scripts
rm -rf "$W"; mkdir -p "$W/udd"
echo "== 1. seed profile with a Chromium-written Bookmarks file"
echo '{"bar":[{"title":"seed","url":"https://seed.example/"}],"other":[],"mobile":[]}' > "$W/seed.json"
node "$HARNESS/exp_a_create.js" "$W/udd" "$W/seed.json" "$W/seed-tree.json" ${BIN:+--bin "$BIN"} 2>&1 | tail -2
BM="$W/udd/Default/Bookmarks"; ls -la "$BM"
echo "== 2. install-chrome (library -> profile)"
python3 -m mhbm --home "$HOME_LIB" install-chrome "$BM" --confirm-chrome-closed
cp "$BM" "$W/written-by-mhbm.json"
python3 -m mhbm verify-chrome "$BM"
echo "== 3. launch Chromium on it, read getTree through the extension"
node "$HARNESS/exp_dump.js" "$W/udd" "$W/tree-after-load.json" "$W/chrome.log" --wait 5000 ${BIN:+--bin "$BIN"}
ls -la "$W/udd/Default" | grep -i bookm
echo "== 4. what Chromium wrote back"
cp "$BM" "$W/rewritten-by-chromium.json"
python3 -m mhbm verify-chrome "$BM"
