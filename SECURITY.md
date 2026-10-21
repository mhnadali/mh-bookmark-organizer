# Security policy

This repository holds Claude Code agents and skills. The one that parses untrusted files, talks to the network and serves a local
web page is **mh-bookmark-organizer** (`.claude/skills/mh-bookmark-organizer`); this policy is mostly about it.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private reporting: **Security tab → "Report a vulnerability"**
(<https://github.com/mhnadali/mh-bookmark-organizer/security/advisories/new>). Include the version (`mhbm --version`), what you did, what
you expected and what happened; a bookmark file, plan file or URL that triggers it is the most useful thing you can attach.
Use synthetic data, never your real bookmarks or credentials.

This is a one-person project: expect an acknowledgement within a few days and a fix or a clear "won't fix, because" within a few
weeks. Reporters are credited in the changelog unless they prefer not to be.

## Supported versions

Only the latest release receives fixes. The library file format is upgraded automatically (with a backup) and refuses to open a
library written by a *newer* version, so staying current is always possible.

## What the tool protects against

| Threat | Defence |
|--------|---------|
| A bookmark file makes the link checker or archiver probe `localhost`, cloud metadata endpoints or the private network (SSRF) | Only `http(s)` to public addresses. Every redirect hop is re-checked. The address that was checked is the address connected to, so a DNS answer that changes in between (DNS rebinding) cannot redirect the request; IPv4 addresses hidden in IPv6 forms (`::ffff:127.0.0.1`, 6to4, Teredo, NAT64, IPv4-compatible, SIIT) and legacy IPv4 spellings (`127.1`, `0177.0.0.1`, `0x7f000001`, `2130706433`) are judged as the IPv4 address, also when a proxy would resolve them. Through an HTTP proxy the proxy resolves names and its own egress policy applies. |
| An archived page runs script or phones home when opened | Snapshots are single files with scripts, frames, objects and event handlers removed and a strict `Content-Security-Policy`. |
| Another web page drives the local web UI (`mhbm ui`) | Binds to `127.0.0.1` only; a random per-run token travels in the URL fragment (a fragment is never sent to the server, so it cannot appear in a log) and must accompany every API call as a header; the `Host` header must be the loopback address and a present `Origin` must match it, which also blocks DNS-rebinding attacks on the UI itself. |
| An MCP client reads or writes arbitrary files | The library is pinned to `--home`. The file tools only reach the directories the server allows (`--allow-dir`, else the folders the client announces as roots, else `$CLAUDE_PROJECT_DIR`, else the working directory; the library folder is always denied). A path is resolved (`~`, `..`, symlinks) and then **opened by the server with `O_NOFOLLOW` and the open descriptor is checked against its real location**, so a file swapped for a link between check and open is refused; only regular files are read, with a size cap; `bm_export` creates a *new* file (`O_EXCL`) and never overwrites. `bm_import_browser` takes a browser name, not a path. Every argument is validated against a strict schema before anything runs and is never coerced (`"false"` is not `false`); free text reaches the command line only as `--flag=value` or after `--`, so a value that starts with `-` stays data. |
| A prompt-injected or confused model changes your bookmarks | Tools that change bookmarks in bulk are split in two: a *plan* tool (`bm_organize`, `bm_dedupe`, `bm_restructure`, `bm_autotag`, `bm_titles`, `bm_prune`) changes nothing and returns a `plan_id`; `bm_apply_plan` applies exactly that stored plan, once, within 24 hours, and only if the library has not changed since (checked again while the write lock is held; undo, link checks and archiving count as changes). Every apply is snapshotted, journaled and undoable (`bm_undo`). Tool annotations (`readOnlyHint`, `destructiveHint`, `openWorldHint`) are accurate, so clients can ask you before the destructive ones. Text that came from the web (titles, notes, page snippets) is marked untrusted in the server instructions; no tool follows instructions found in it. |
| An MCP tool call that runs away or leaks | Each call runs in its own child process with an allow-listed environment (no tokens exported in your shell), a time limit and cancellation (SIGINT, then SIGTERM, then SIGKILL; operations are transactional, so an interrupted apply leaves the old state or the new one). The protocol stream is private: a stray `print()` or a child process cannot corrupt it. Messages, results, files and the rate of calls are size- and rate-capped. `--read-only` hides every tool that writes **and** opens the library with SQLite `mode=ro`, so it changes nothing on disk (no migration, no model cache). `--no-network` hides the tools that contact other hosts. |
| A malicious or malformed bookmark, plan or rules file | Parsed defensively and validated before anything changes; a rejected plan entry changes nothing; fuzz tests feed the parsers garbage. |
| A rule or configuration that hangs or leaks | `title_regex` rules that can backtrack exponentially (a repeated group holding another unbounded repeat, back-references, patterns over 200 characters) are refused and titles are matched only up to 400 characters; `config` hides every value that looks like a secret (passwords, tokens, keys, webhooks, the login part of a URL). |
| Spreadsheet formula injection from CSV export; path traversal from Markdown/Obsidian export | Cells that start with a formula character are neutralised; exported file names are confined to the target directory. |
| Losing data to a bug, a crash or two processes at once | Destructive commands are dry runs unless you pass `--apply`, and every apply is journaled, snapshotted and undoable; the database uses write-ahead logging with a lock wait; an upgrade of an older library file is done in one transaction after a copy is saved; browser profiles are never written without `--confirm-chrome-closed`. |
| Secrets in files | SMTP password and webhook URL are read from environment variables, never from `config.json`; proxy credentials are never printed. |
| Tampered build pipeline | GitHub Actions are pinned to commit SHAs, the workflow has read-only permissions, `persist-credentials` is off, and the tool has no runtime dependencies (standard library only; numpy is optional). |

## What it does not protect against (out of scope)

- Anything that already runs code as you on your machine: it can read your library and your browser profile directly.
- The browser extension's native host trusts the extension IDs you registered with `install-native-host`.
- Pages you open from `archive/` are sanitised, but you should still treat them as untrusted content.
- A hostile proxy or a hostile network between you and the sites you check. HTTPS certificates are verified by Python's
  defaults, but the tool is a bookmark organiser, not a privacy tool.
- Denial of service by an enormous bookmark file or a site that answers very slowly: sizes and timeouts are capped, but this is
  a local tool and not hardened as a service.

The deliberately relaxed network guard used by the test suite (`netsafe.testing_allow_private`) is not reachable from the command
line, the environment, the MCP server or the web UI. A way to reach it from outside the test suite is a vulnerability: please report it.

## Hardening you can do

- Run `mhbm doctor` after installing; it checks the guard, the library and the setup.
- Give MCP clients `--read-only` unless they must change things (`--no-network` too if they should not contact other sites), and a dedicated `--allow-dir`.
- Keep the library in a directory only you can read (`chmod 700`), because it contains your browsing history in the form of bookmarks.
