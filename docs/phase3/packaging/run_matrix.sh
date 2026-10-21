#!/bin/bash
# usage: run_matrix.sh LABEL ARTIFACT [EXTRAS] [SMOKE_ARGS...]
#   LABEL     wheel|sdist (only used to name the venv)
#   ARTIFACT  path to the .whl or .tar.gz
#   EXTRAS    "" or "[fast]"
# For every Python 3.9..3.13: fresh venv (with pip), pip install of the artifact, smoke test from a temp cwd outside any repo.
set -u
P=/tmp/claude-0/-home-user-adding-skills/e94ea44a-2022-55bc-9ae0-034b4c065efe/scratchpad/phase3/packaging
export PATH=/root/.local/bin:$PATH PYTHONDONTWRITEBYTECODE=1
LABEL=$1; ART=$(readlink -f "$2"); EXTRAS=${3:-}; shift; shift; shift 2>/dev/null || true
FIX=${FIXTURE:-/home/user/adding-skills/.claude/skills/mh-bookmark-organizer/tests/fixtures/messy-library.html}
PYS=${PYS:-"3.9 3.10 3.11 3.12 3.13"}
rc=0
for v in $PYS; do
  d=$P/venvs/t-$LABEL${EXTRAS//[^a-z]/}-$v
  rm -rf "$d"
  uv venv --python $v --seed -q "$d" 2>/dev/null
  echo "=== $LABEL$EXTRAS on python $v"
  ( cd /tmp && "$d/bin/python" -m pip install --disable-pip-version-check -q "${ART}${EXTRAS}" 2>&1 | tail -15 ) || { echo "pip install FAILED"; rc=1; continue; }
  numpy=no; [ -n "$EXTRAS" ] && numpy=yes
  ( cd /tmp && "$d/bin/python" $P/smoke_installed.py --fixture "$FIX" --numpy $numpy "$@" ) || rc=1
done
exit $rc
