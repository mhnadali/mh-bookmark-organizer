# Changelog

Versions follow the `mhbm.__version__` string; the browser extension has its own `manifest.json` version, kept in step.

## Unreleased (1.6.0 in progress)

**OpenAI Codex**
- The skill folder is now also a Codex plugin: `.codex-plugin/plugin.json`, the MCP server `bookmarks` (`.codex-mcp.json`) and a Codex-written skill
  (`codex/skills/mh-bookmark-organizer/`, MCP-first, same safety rules, with `agents/openai.yaml`). Codex reads the existing marketplace file, so
  `codex plugin marketplace add mhnadali/mh-bookmark-organizer` and `codex plugin add mh-bookmark-organizer@mhnadali-skills` install it.
- Why not the Claude Code files as they are: Codex ignores a root `SKILL.md`, does not fill `${user_config.*}` or `${CLAUDE_PLUGIN_ROOT}` (the server
  was registered with the placeholders as literal text), accepts only a bare command name and a plugin-relative `cwd`, and passes a server just
  `HOME`, `PATH` and a few more variables. The new launcher `scripts/codex_mcp.py` (`mhbm/mcp/codex.py`) supplies safe defaults: library
  `~/.mh-bookmark-organizer` (or `MHBM_HOME`), file access limited to `Downloads`, `Documents` and `Desktop` that exist (or `MHBM_MCP_ALLOW_DIRS`), and
  the module path pinned to the engine so a folder the server works in cannot replace `mhbm`.
- `tools/release_check.py` and the tests now require the Codex manifest to carry the release version too. `tests/test_codex.py` (15 tests) runs the real
  Codex when it is installed (plugin install, server start, 29 tools, a tool call). Checked with Codex CLI 0.163.0.
- The Claude Code validator test ignores the new "plugin directory" advisory (licence, icon) of recent Claude Code versions; the licence stays the owner's decision.

**Chromium write-back, checked against a real browser** (docs/phase3/real-chromium; 11 tests drive headless Chromium 141 with a probe extension)
- The checksum, ids, GUIDs, dates and order of the `Bookmarks` file we write and patch matched Chromium's own byte for byte, and the file still
  verifies after the browser rewrites it. The same run found defects that no test of ours could see; all fixed:
  - a URL Chromium cannot parse (`example.com`, `http://`, bad port) is silently dropped by the browser, and the next `sync --apply` then
    deleted that bookmark (tags, notes) from the library; it is now kept in the library, reported as not syncable, and left out of the file;
  - library folders without a root name (`Work/...`, unfiled) never matched the browser's `Other bookmarks/...`, so every sync deleted and re-added them
    with a new id and GUID each time;
  - URLs the browser re-encodes (spaces, non-ASCII, IDN, `..`) had another sync key (churn and wrongful deletions); keys now use the browser's form
    (checked on 4,000 generated URLs);
  - a running browser is detected (`SingletonLock` with a live pid, or Windows' `lockfile`) and `install-chrome`/`sync` refuse, instead of being overwritten at
    the browser's next save while the library believes the write happened;
  - `sync` turned the private `Bookmarks` file (0600) into 0644, and `install-chrome` wrote in place (a torn file means silently empty bookmarks): both now
    write a temp file, fsync, keep the permissions and rename;
  - `verify-chrome` accepted a wrong `version`, bad ids and unparseable URLs, crashed on a node without an id or a non-string name, and rejected a UTF-8 BOM
    that Chromium accepts; 98 or more nested folders make Chromium discard the whole file and are refused (limit 90).
- `install-chrome` still writes alphabetically (the library does not keep the browser's order) and says so; `sync` patches in place and keeps the order.
- New: `tests/test_chromium_guards.py` (17, always run), `tests/test_realbrowser.py` (11, need Node + a browser; `CHROMIUM_BIN` picks any Chromium-family binary).

**Firefox and other Gecko browsers, checked against real Gecko** (Firefox 157.0.1, Zen 1.23.2b and Floorp 12.20.1 run headless with no network)
- **Linux profile location.** Firefox 147+ keeps a fresh profile in `~/.config/mozilla/firefox` (`$XDG_CONFIG_HOME/mozilla/firefox`); only
  `~/.mozilla/firefox` was searched, so `browsers` and `import-browser firefox` found nothing on a new install. Both are searched now,
  plus Snap and Flatpak (also the XDG form inside Flatpak).
- **The right profile.** The first `places.sqlite` in alphabetical order was used, which on a machine with a leftover `xxxx.default` next
  to the real `xxxx.default-release` is a coin toss. `installs.ini` / the `[Install...]` section of `profiles.ini` now decide (then
  `Default=1`, the other listed profiles, and unlisted profile folders newest first); `IsRelative=0` paths and names with spaces work.
- **New:** LibreWolf, Waterfox, Floorp and Zen (`import-browser zen`, ...); `--profile` accepts a profile folder, a renamed database, or a
  Firefox `.json` backup. A database that is not SQLite or is damaged is a normal error, not a traceback.
- **A running Firefox.** The database and its `-wal` are copied while Firefox may checkpoint in between; a copy of the old database with the
  new WAL is a malformed database (or silently misses recent bookmarks). The copy is verified (database mtime/size and the 32-byte WAL
  header before and after), retried, and refused with a clear message if the browser never settles. The `-shm` is no longer copied.
- **Backups agree with the database.** A `bookmarks-*.jsonlz4` / JSON backup named the Other Bookmarks root `unfiled` (so importing the same
  profile from both sources duplicated those bookmarks) and imported `place:` saved queries as bookmarks.
- **Smaller fixes:** a temp folder whose path contains `#`, `%` or `?` broke the database open; a millisecond `ADD_DATE` that Firefox stored
  times 10^6 gave a date in the year 52000; the LZ4 decoder checked the output size only after copying a match (a 5 MB file of `0xFF`
  claimed over a gigabyte) and is now checked first and about twice as fast; the HTML export marks `Other bookmarks` so Firefox files it in
  its own Other Bookmarks root instead of a sub-folder of the menu.
- **Tests.** Fixtures written by real Firefox 157 (`tests/fixtures/firefox/`), layouts of real Firefox/Zen/Floorp profile folders, a deterministic
  db/WAL race, and `tests/test_real_firefox.py`, which drives a real browser when `MHBM_FIREFOX_DIR` names one (skipped otherwise; 5 live tests passed on
  the pinned Firefox 157.0.1). `import-browser` and the MCP tool share one browser-name list, so the Gecko forks work over MCP too.

**Profile discovery and write safety** (docs/phase3/profiles-audit; 83 tests on synthetic machines that pretend to be Linux, macOS, Windows and WSL)
- **More browsers and places.** Chrome/Edge/Brave/Vivaldi/Opera channels (Beta, Dev, Canary/SxS, Nightly), Opera GX and Yandex, Snap and
  Flatpak installs, `XDG_CONFIG_HOME` / `CHROME_CONFIG_HOME`, `LOCALAPPDATA`/`APPDATA` redirects, the Windows side of WSL, and the home of the
  person who ran `sudo`. A folder reachable through several symlinks is listed once. Arc is recognised and explained (its bookmarks are not in a
  Chromium file).
- **The right profile.** Before, `import-browser chrome` took the first folder in alphabetical order (`Guest Profile` came before `Default`).
  Now: the profile the browser last used (`Local State`), then `Default`, then `Profile 2` before `Profile 10`; `Guest`, `System` and
  ephemeral profiles are skipped. `--profile` takes a folder name, the name shown in the browser (`Work`), a folder or a file (it was documented
  but failed on `"Profile 1"`); `--all-profiles` imports every profile; the output says which files were read and how many other profiles exist.
- **Account bookmarks and unreadable stores.** `AccountBookmarks` (bookmarks of a signed-in Chrome profile) is read and merged with `Bookmarks`;
  `Bookmarks.bak` is a fallback only; a profile that keeps its bookmarks in `EncryptedBookmarks2`, or Safari without Full Disk Access, is
  listed with a note saying why instead of vanishing or raising. `browsers` shows each profile's name, folder, `default`/`signed in` flags and
  notes; names that are e-mail addresses are never printed. Safari dates were read in the local time zone instead of UTC.
- **Errors are messages.** A file the browser holds open, a damaged `places.sqlite`, a binary file or a full disk give `error: ...` and exit
  code 2, not a traceback; a torn copy of a live database is retried before giving up.
- **Writing into a profile (`install-chrome`, `sync`).** New `profilesafe` module: a running browser is detected by its lock (and a lock
  from another computer, or inside a Snap/Flatpak sandbox, counts as running because it cannot be checked); Chrome Sync profiles
  (`sync_metadata`) are refused unless `--allow-synced-profile`; `--ignore-profile-lock` overrides a wrong lock; backups get a unique name
  (two runs in the same second used to overwrite the backup of the original: the original was lost); writes go to a temp file, are
  fsynced, keep the mode and the owner (run as root, the new file used to become root's), follow a symlink instead of replacing it, and
  explain a Windows sharing violation; the browser file is fingerprinted and a change while mhbm worked aborts before anything changes;
  `install-chrome FILE --restore [BACKUP]` undoes a write (and brings back an `EncryptedBookmarks2` that was moved aside); `sync` refuses to
  patch `Bookmarks.bak`, which no browser reads, and checks the file can be patched before it changes the library. `checksum_sha256`
  (written by newer Chromium builds) is verified and kept true.
- Not verified on real hardware: Chrome with Sync or `AccountBookmarks` (the sync-metadata and SHA-256 handling follows Chromium's source and
  is tested on synthetic files), macOS, Windows, Snap and Flatpak layouts.

**MCP server, rebuilt (`mhbm.mcp`; tool names and arguments change)** (docs/phase3/mcp-audit.md; 83 tests, three golden transcripts, `claude mcp list` shows *Connected*)
- **Breaking, stated plainly.** The 1.5.x server could not even start (see 1.5.2), so no client can depend on it, but its tool set is gone: 29 tools
  replace the 14. The `apply: true` flag no longer exists. Bulk changes are *proposed* by a plan tool (`bm_organize`, `bm_dedupe`, `bm_restructure`,
  `bm_autotag`, `bm_titles`, `bm_prune`), which changes nothing and returns a `plan_id`; `bm_apply_plan(plan_id)` applies exactly that plan, once,
  within 24 hours, only if the library has not changed since. New: `bm_folders bm_tags bm_history bm_reading_list bm_feedback bm_rules
  bm_rules_edit bm_browsers bm_backups bm_doctor bm_learn bm_tag bm_mark bm_apply_plan`, 6 prompts (slash commands in Claude Code) and 4 resources
  plus 2 resource templates. `mhbm-mcp` / `python -m mhbm.mcp` start it without importing the rest of the CLI; `mhbm mcp` is the same server.
- **Protocol.** Revisions 2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25 are negotiated and the reply is shaped for the revision
  (annotations, titles, output schemas, server description). Tool results carry `structuredContent` next to the text, with `isError` for tool
  failures and JSON-RPC errors only for protocol faults. Requests are answered while a tool runs (`ping`, `tools/list`), `notifications/cancelled`
  stops the child process, `progressToken` gets progress (a heartbeat when the command reports none), client roots define the allowed
  directories, malformed input of every kind (non-UTF-8, 5 MB lines, unbalanced nesting, `NaN`, bad ids, batches) costs one reply and never the
  session.
- **Safety.** Every argument is checked against a strict schema before anything runs and is never coerced (`"false"` is not `false`); free text reaches the command line only as `--flag=value` or after `--`. File tools open the
  file themselves without following links and check the open descriptor, which closes the race the audit found; they never overwrite, never
  write inside the library, and ignore `~` tricks. Each call runs in a child process with an allow-listed environment, a time limit and
  SIGINT/SIGTERM/SIGKILL cancellation; the protocol stream cannot be written to by anything else. Results are size-capped without breaking the
  JSON. Error texts do not carry the library path.
- **`--read-only` is now read-only.** It hides every tool that writes and opens the library with SQLite `mode=ro` (`Library(read_only=True)`, also
  `MHBM_READONLY=1`): no folder is created, an old layout is refused instead of upgraded, the model cache is read but not written. Also new:
  `--no-network`, `--disable-tool`, `--max-result-chars`, `--no-client-roots`.
- A relative `--home` (including the default `.mhbm`) works for plan files; a source checkout is found by the child processes without installing.

**Claude Code plugin and marketplace** (`/plugin marketplace add mhnadali/mh-bookmark-organizer`, `/plugin install mh-bookmark-organizer@mhnadali-skills`)
- The skill folder is the plugin (`.claude-plugin/plugin.json`, `.mcp.json`, `agents/`), so there is one copy of the engine, the skill and the
  documentation; `.claude-plugin/marketplace.json` at the repository root lists it. `claude plugin validate --strict` passes for both, and the
  plugin was installed into an empty Claude Code configuration, where its skill, subagent and MCP server (all 29 tools) loaded and connected.
- Enabling it asks for: the Python command, the **library folder** (outside the plugin cache, so updates and uninstalling never delete it), a
  read-only switch (default off) and a no-network switch. Both switches reach the server as `MHBM_MCP_READ_ONLY` / `MHBM_MCP_NO_NETWORK`.
- `SKILL.md` finds its scripts through `${CLAUDE_SKILL_DIR}` (it said `.claude/skills/mh-bookmark-organizer/scripts`, which is wrong once installed)
  and the agent no longer names that path. Inside this repository the skill folder would also load as a `@skills-dir` plugin, so the manifest
  says `defaultEnabled: false` and only the marketplace entry turns it on.
- No `license` field: the owner has not chosen one (`tests/test_plugin.py` enforces that until a LICENSE file exists).

**PyPI package and release pipeline** (nothing is uploaded yet: a licence has to be chosen first)
- `pyproject.toml`: classifiers for Python 3.9-3.13, project URLs, the `mhbm-mcp` console script; `MANIFEST.in` makes the sdist the whole skill folder
  (tests and fixtures, extension, plugin files, documentation) while the wheel stays the engine only (66 files, no dependencies). README links are
  absolute so PyPI shows them. Checked with `twine check --strict`, `check-wheel-contents` and `validate-pyproject`; a second build is byte-identical.
- **Installed and used, not just built:** `tools/smoke_installed.py` installs the wheel, the sdist and the wheel with `[fast]` into fresh virtual
  environments on Python 3.9, 3.10, 3.11, 3.12 and 3.13 (15 runs, 12 steps each: console scripts, `doctor`, import, search, `learn`, the MCP server over
  stdio read-only and full, the web UI) and the whole test suite runs from the unpacked sdist.
- `.github/workflows/release.yml`: a version tag runs verify (tag = code = manifests = README = CHANGELOG, on the default branch), build (hash-pinned
  tools in `requirements-release.txt`), the sdist's own tests, an install matrix, build-provenance attestation, TestPyPI, install from TestPyPI, PyPI
  (a person approves the `pypi` environment) and a GitHub release with checksums. Uploads use PyPI trusted publishing (OIDC): no stored secret.
  `test.yml` gained a `package` job. Every action is pinned to a commit SHA that was resolved from the upstream tag. `RELEASING.md` lists what only the
  owner can do (licence, PyPI/TestPyPI pending publishers, GitHub environments, private vulnerability reporting).
- The release gate (`MHBM_RELEASE=1`) fails without a `LICENSE` file and licence metadata, so a release cannot go out by accident.

**Fixes from the adversarial review of all of the above** (five independent reviewers, each finding re-checked by a skeptic that tried to reproduce it: 34 confirmed, none refuted)
- **MCP.** A plan is now judged *under the write lock*: before, `bm_apply_plan` could start while a long import was still running (or two applies at once)
  and both would go through. The plan fingerprint covers undo, link-check results and the archive, not only the newest journal entry (a prune plan for links
  that had since been re-checked alive was accepted). Tools declared "default 50" ran unbounded when called without the argument (`bm_check_links`, `bm_archive`):
  declared defaults are applied now. A client answer with an object or list for an id, or a malformed `roots/list` reply, ended the whole server; an empty
  root list now revokes the allowed folders instead of being ignored. `bm_doctor` returned an empty error exactly when a check failed (exit 2) and, in
  `--read-only`, wrote a probe file into the library; its answers (and every structured answer) are redacted of the library path. NUL or lone-surrogate text in an
  argument became an internal error. `resources/read` is inside the concurrency and rate limits; queued imports no longer each copy their file before waiting.
  `--disable-tool` with an unknown name stops the server instead of leaving the tool exposed; the instructions and prompts no longer send the model to tools the
  options removed. The plugin starts the server without the project folder on the module path (a project with its own `mhbm/` folder would have run its code).
- **Library.** `op()` stayed "inside an operation" after a busy database (later changes were not journaled or undoable); `undo`, `backups --restore` and
  `set_folders` are single transactions; `backups --restore` checks the file first (an `export` file used to empty the library and report success);
  the folder list is journaled from inside the write lock; a command that read the library and writes it back now refuses if another process changed it
  meanwhile instead of silently overwriting that change; read-only mode no longer misreads a folder name containing `#`, `?` or `%` (it opened, and created, another file).
- **Rules and network.** `title_regex` guard: alternation, bounded and sequential repeats and `(?x)` got past it; matching now also has a 0.25 s deadline per rule where
  the platform allows (POSIX), and a rule that exceeds it is skipped with a warning. Percent-escaped hosts (`http://%31%32%37.0.0.1/`) are refused.
- **Browser profiles.** `sync` recorded a *re-read* of the browser file as its baseline, so a bookmark the browser saved mid-sync was deleted from the browser at the
  next sync; bookmarks the browser cannot tell apart no longer resurrect or vanish together (`kept_local_same_address`); `sync` refuses folders nested deeper than
  Chromium accepts; `export --format chrome --out <profile>/Bookmarks` is refused (use `install-chrome`) and all `--out` files are written atomically; backups are
  written under a temporary name and `--restore` refuses an empty or rootless backup; `verify-chrome` reports malformed trees and absurd nesting instead of a traceback;
  WSL discovery survives one unreadable Windows user folder and prefers the current user's.
- **Release.** The release gate looks for the licence where the build can see it (the skill folder); version matching is whole-word (`1.5.20` is not `1.5.2`),
  and post/dev tags are not treated as releases.
- Not changed, on purpose: the `(\d+\.){3}\d+`-style patterns the screen refuses although they are safe; a rule that is slow on Windows is not interrupted there.

**Fixes found by the Phase 3 audit** (each reproduced before it was fixed)
- **An operation is one transaction.** `Library.op()` journaled an operation but committed every write on its own, so a crash, `Ctrl-C` or kill
  halfway through `apply-plan --apply` (or any other write command, the web UI, the extension) left a half-applied change: in the audit 5 of 18
  random interruptions left 14,400-18,400 of 20,000 rows, and the SIGTERM/SIGKILL ones were not even journaled, so not undoable. Now the writes and
  their journal entry commit together, or not at all; an exception rolls everything back. Link checking and archiving only record what they observed
  (status, page text) and are not in the undo journal.
- **Odd spellings of private addresses behind a proxy.** `127.1`, `0177.0.0.1`, `0x7f.0.0.1`, `2130706433` and `10.1` were not recognised as IP
  addresses by the SSRF guard and were handed to the proxy as if they were host names (the proxy resolves them to loopback on its own network).
  They are now read the way `inet_aton` reads them and refused.
- **`config` printed secrets.** `notify` settings (an SMTP password, a webhook URL, API keys, the login part of a URL) were shown verbatim; they
  are hidden now.
- **`title_regex` rules could hang the program.** A rule such as `(a+)+$` takes exponential time on an unlucky title. Rules whose repeated group
  contains another unbounded repeat, back-references and patterns over 200 characters are refused with an explanation, and only the first
  400 characters of a title are matched.
- **`organize`, `triage` and `restructure` crashed** (`IndexError`) on a library where no folder could be judged on held-out data, for example
  every bookmark in a folder of its own. They now use the fixed, uncalibrated blend and say so.

## 1.5.2

**Hotfix: the MCP server could not start** (found by the Phase 3 audit, which launched the real command; the audit is in `docs/phase3/mcp-audit.md`)
- `mhbm mcp` failed with `ImportError: cannot import name 'mcp_server' from 'mhbm.commands'` in every release since the commands were split
  out of `cli.py` (1.2): the import was `from . import` instead of `from .. import`. The tests called `mcp_server.handle()` directly and never
  started the command, so nothing noticed. A test now starts the real command and talks to it over stdio.
- Now that it starts, the gaps the audit found in the old tool layer are closed in the small: arguments are checked against each tool's schema
  (unknown keys, wrong types, out-of-range numbers, bad enum values are refused as tool errors); `apply` must be exactly `true` (before, `"false"`
  or `1` counted as true and wrote); `bm_import_browser` takes a browser name from a fixed list instead of any file path (it bypassed `--allow-dir`);
  option-like values (`-x.html`, a query starting with `-`) stay values; one malformed message (a batch, a bare value, wrong-typed `params`) no longer
  ends the session; an unknown tool is reported as invalid params (-32602), not "method not found".
- README and SECURITY.md now say which tools act immediately (import, export, archive, check, undo) and what `--read-only` really guarantees.
- Still open, and planned for 1.6.0: preview-then-apply for every writing tool, the path-check race, cancellation/progress, tool annotations,
  structured results, resources and prompts. See the audit for the full list.

## 1.5.1

**Phase 0: close the risks** (no change to filing; nothing to relearn)
- **DNS rebinding.** The link checker and archiver used to resolve a host name, check the address, and then let the HTTP library
  resolve it again to connect: a name that answered "public" the first time and "127.0.0.1" the second could reach the local
  network. The check is now made inside the connect call, on the very addresses that are connected to (every answer must be public,
  a mixed answer is refused); TLS still verifies the certificate against the host name. IPv4 addresses hidden in IPv6 forms
  (`::ffff:127.0.0.1`, 6to4, Teredo, NAT64 `64:ff9b::/96`, the old IPv4-compatible and SIIT forms) are judged as the IPv4 address,
  and the deprecated site-local range `fec0::/10`, which Python still calls global, counts as private. Proxied requests are unchanged (the proxy resolves names).
- **Several processes, one library.** The database now uses write-ahead logging and waits up to 30 s for a busy lock instead
  of failing (`MHBM_BUSY_TIMEOUT_MS`; `MHBM_JOURNAL_MODE=delete` opts out of WAL for file systems that cannot share memory). A
  command that still cannot get the lock says "the library is busy" and exits with code 75. Recording an operation and `undo`
  take the write lock first, so two processes cannot both undo the same operation or journal a half-seen state.
  Opening an up-to-date library no longer writes anything (it used to re-stamp the version every time, so even `search` could wait behind a writer).
  A stress test runs four writers, two readers and three CLI calls at once and checks that no write is lost and the index and journal agree.
- **Versioned library file.** `PRAGMA user_version` now drives a numbered upgrade ladder (`mhbm/schema.py`). An older library is copied to
  `backups/library-schema<N>-<time>.db` first, then upgraded in a single transaction (all or nothing); a library written by a *newer*
  mhbm is refused untouched instead of being silently stamped down. A missing table or column is repaired on open. Tested against files
  written by the real first release and two version-2 releases (`tests/fixtures/old-library-*.sql`, generated from git history).
- **Fixed on the way:** a library that failed to open left its file open; `doctor` compared only the *number* of search-index entries
  (an index pointing at the wrong rows looked healthy) and now compares row ids; `doctor --fix` vacuums and then rebuilds the index,
  because a vacuum may renumber the rows the index is keyed by; the numpy import no longer depends on which mypy you have.
- **Supply chain.** Every GitHub Action is pinned to a full commit SHA (checkout 7.0.1, setup-python 7.0.0, setup-node 7.0.0, all on Node 24),
  checkout no longer keeps credentials, jobs have timeouts and superseded runs are cancelled; ruff, mypy and coverage are pinned in
  `requirements-dev.txt`; Dependabot proposes updates to both. `tests/test_repo_hygiene.py` fails if a pin is loosened or the versions disagree.
- `SECURITY.md`: what is defended, what is out of scope, how to report privately.
- New test files: `test_concurrency.py`, `test_migrate.py`, `test_repo_hygiene.py`, and DNS-rebinding cases in `test_network.py`.

## 1.5.0

**Phase 2: speed and scale** (no change to what the filing model answers: the reports on the real library are identical)
- Training the filing model on 1,422 real bookmarks: 47 s -> 9 s (4 cores, numpy) or 41 s (pure Python, one process).
  - optional numpy (`pip install .[fast]`) vectorises the combiner's fit; it agrees with the pure-Python path to ~1e-15
  - the cross-validation folds run in parallel worker processes (spawned, so it is safe next to the web UI's and
    native host's threads; falls back to one process if the sandbox forbids them; `MHBM_WORKERS` overrides)
  - features are memoised during training, the TF-IDF work is shared by the neighbour and centroid scorers, the shuffle
    no longer runs a Python loop
- Big libraries: judging is sampled above ~6,000 filed bookmarks, posting lists are capped, the name-overlap and
  naive-Bayes scorers no longer scan every folder per query; 45,000 bookmarks train in about 2 minutes (129 s) and are answered in 15 ms each.
- `restructure` is indexed: 45,000 bookmarks in 0.4 s instead of 16 s, with exactly the same proposals (proved against a
  frozen copy of the old code on varied libraries). Its proposal order is now stable; before it depended on the process.
- A small library's combiner could stop too early, so one bookmark's confidence moved between 0.7 and 1.0 depending on batch
  order; small problems now always get at least 600 optimisation steps.
- Complexity is enforced (`ruff` C901, 14): `restructure.analyse`, `fuzzy_groups`, `verify_chrome`, `autotag.suggest`,
  `cmd_dedupe`, `apply_plan`, the snapshot sanitiser and the interactive triage loop were split into small functions.
- `python -m mhbm` no longer runs the command line again inside a worker process (the module is guarded).
- `doctor` reports whether numpy is in use; CI runs the suite with numpy too and times big-library training.

## 1.4.0

**Phase 1: learn from what you do, decide in groups**
- `feedback`: every suggestion is remembered, and what you do with it is recorded (accepted, corrected, rejected) from the
  review page, the web UI, `triage`, the extension's save button and plan files. Undoing an operation forgets its decisions.
  Only decisions you looked at one by one count as evidence; a plan file applied blind is recorded but not counted.
- The model's quoted confidence is checked against those explicit decisions (a banded Beta posterior, monotone, starting from
  "the calibration is right" so a few decisions cannot swing it) and a folder you turned down for a bookmark is never
  suggested for it again.
- `triage`: unfiled bookmarks are grouped (same site and the same best guess, saved together, user rules) and each group gets
  one suggestion. `--interactive` walks through them in the terminal and feeds every decision back into the model;
  `--plan-out` writes a reviewable plan; the web UI has a Triage tab. In a real 1,500-bookmark library a simulated user needed
  roughly a third to 40% fewer decisions than going one by one.
- `learn --curve` (accuracy as the filed data grows) and `learn --text-effect` (what archived page text adds; run `archive` first).
- MCP: read-only `bm_triage` and `bm_feedback`. `doctor` reports the feedback record.

**Filing quality**
- Candidates now include the siblings, parent and children of the best guesses, with tree-relation features: more of the
  right answers are reachable in a deep tree (about +2 points of coverage at the 80% target, precision within a point).
- Training is fully deterministic: it no longer depends on the process's hash seed, so the same library gives the same
  suggestions every day (before, two runs could differ by a couple of points).
- The ranking judges bookmarks saved together together (`top1_pooled` in `learn`), which `triage` uses.

**Fixed**
- The extension's suggest call crashed (`math domain error`) on a page whose words appeared in your library but never side by side.
- A decision that changed no bookmark was not journaled, so it could not be undone.
- Isotonic calibration looked up tied inputs in whichever block came first; equal inputs are now pooled.

## 1.3.0

**Filing accuracy** (the headline of this release)
- The filing model is now a calibrated stack: TF-IDF nearest neighbours (page text and URL), class centroids, naive Bayes,
  time-of-saving and "sitting" scorers, per-host affinity, combined by a logistic stacker and an isotonic calibrator.
  Evaluation is cross-validated with *whole saving sessions* held out, so the reported numbers are not flattered by
  neighbours saved in the same minute.
- Answers carry a calibrated probability. Below the threshold it answers one or two levels up the tree
  ("sure of the area, not the sub-folder") instead of guessing; vacuous parents ("Bookmarks bar") are never offered.
- `learn --seeds N` repeats the measurement on N different splits and reports the spread.
- Absolute-evidence features and a per-candidate calibration stop nonsense bookmarks from getting a confident answer.

**New**
- Your own filing rules: `rules add|list|remove|suggest|test` (host, URL prefix, title regex, keyword), applied before the model.
- `tags`, `synonyms`, `autotag`, `titles`, `canonical` (tag hygiene, keyword suggestions, title clean-up, canonical URLs).
- Reading list: `mark`, `reading-list`; favourites; Pocket/Instapaper read state survives import.
- `doctor [--fix] [--network]`, guided `wizard`, offline plan `review` page.
- Exporters for Raindrop, Pocket, Linkwarden, Karakeep, Instapaper (plus HTML, Chrome JSON, CSV, JSON, Markdown, Obsidian).
- Two-way `sync` with a Chromium `Bookmarks` file (patched in place, baseline-aware), and `verify-chrome`.
- Browser extension (Manifest V3) with a native-messaging host: file new bookmarks where you would, search, organize in place.
- Local web UI (`ui`): loopback only, per-run token, strict CSP.
- `maintain --notify` and `notify`: desktop, e-mail or webhook summaries of scheduled maintenance.
- Localised messages (`--lang en|fa|ar|es|fr|de`, RTL-safe), zsh and fish completion alongside bash.
- MCP: `--read-only`, and `--allow-dir` confines `bm_import_file` / `bm_export` to chosen directories.

**Fixed (found by the new seeded fuzz tests and the real-library review)**
- Parsers raised `TypeError`/`AttributeError`/`RecursionError` on malformed files; they now raise `FormatError`.
- Truncated Firefox `.jsonlz4` backups raised `IndexError`; now `FormatError`.
- A plan entry that was rejected (bad URL, too-deep folder) had already been partly applied in memory; entries are now
  validated first and applied as a whole. Wrong types are refused (a list as `folder` used to crash, a `null` folder silently unfiled).
- No-break spaces in titles were collapsed on HTML import.
- The CSV formula guard was not reversible for values that start with an apostrophe.
- Our own JSON export now restores folder names exactly (trailing spaces survive a round trip).
- Re-importing the same export doubled the library; a bookmark already in the same folder is now skipped (`--allow-duplicates` to override).
- Undo restores empty folders; the undo journal stores only changed columns (about 0.2 KB per changed bookmark).
- Folder names with trailing spaces were rewritten, which created twin folders; names are kept exactly as written.

**Engineering**
- `ruff` and `mypy` are clean; coverage is about 90% (`fail_under = 88`); the suite runs on Python 3.9 to 3.13.
- Seeded fuzz and round-trip tests for every parser/writer; an anonymised synthetic "messy library" fixture with a generator.
- The test-only SSRF exemption is now a scoped context manager (`netsafe.testing_allow_private()`), not a module flag.

## 1.2.0
Fixes from testing against a real 1,500-bookmark Chrome export.
- Link checks behind an HTTP proxy no longer fail every link as a DNS error; if nothing in a batch answers (offline,
  firewall, proxy) nothing is recorded against the links.
- Folder names are preserved exactly (trailing spaces); plans resolve onto existing folders instead of creating twins.
- Undo restores the folder set (empty folders included). `clean-urls` only removes universally meaningless parameters.
- `restructure` no longer flattens deep trees by default (it proposed moving 78% of a real library); `--max-depth` is opt-in.
- `organize`: taxonomy rules cannot create a parallel folder tree in an established library.
- Far fewer false-positive fuzzy duplicates; re-importing an export no longer doubles the library; `dedupe` notes
  "also filed in" and has `--same-folder-only`.
- Linear-time fuzzy matching: 50,000 bookmarks load, train, propose and restructure in seconds.

## 1.1.0
- Link checks no longer condemn a link on one failure (retries, per-host throttling, "dead" only after repeated failures over time).
- Learned filing from the user's own folders; reviewable plan files, undo journal, history, per-operation snapshots.
- Wayback lookups, robots.txt-aware archiving, sanitised single-file snapshots, PDF text.
- Variant (mobile/AMP/canonical) and fuzzy duplicates with interactive review; search operators and an incremental FTS index.
- Folder-tree restructuring; direct import from Chromium-family browsers, Firefox and Safari; more importers; Obsidian export.
- Offline dashboard, MCP server, `maintain`/`schedule`, config file, shell completion, `--json` everywhere, packaging and CI.
- CSV export neutralises formula injection; Markdown/Obsidian export escapes and contains paths; collision-free ids.

## 1.0.0
- First release: import/export (Chrome, Firefox, Netscape HTML, CSV, JSON), URL-normalised dedupe, SSRF-guarded link checks,
  page archiving with FTS5 search, rule-based and AI-assisted tagging, snapshot-protected dry-run workflows.
