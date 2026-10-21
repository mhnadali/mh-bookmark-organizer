# source this: defines cc (claude with a clean environment) and cco (same, with no network namespace)
CLAUDE_BIN=${CLAUDE_BIN:-/opt/node22/bin/claude}
cc()  { env -i PATH=/opt/node22/bin:/usr/local/bin:/usr/bin:/bin HOME="$H" CLAUDE_CONFIG_DIR="$H/.claude" TERM=dumb LANG=C.UTF-8 "$CLAUDE_BIN" "$@"; }
cco() { unshare -rn env -i PATH=/opt/node22/bin:/usr/local/bin:/usr/bin:/bin HOME="$H" CLAUDE_CONFIG_DIR="$H/.claude" TERM=dumb LANG=C.UTF-8 "$CLAUDE_BIN" "$@"; }
