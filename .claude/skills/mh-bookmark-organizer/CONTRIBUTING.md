# Contributing

The engine is standard-library Python 3.9+ with no runtime dependencies. Keep it that way: a new dependency needs a very
good reason, and optional tools (like poppler's `pdftotext`) must degrade gracefully when absent.

## Setup

```bash
cd .claude/skills/mh-bookmark-organizer
python3 -m venv .venv && . .venv/bin/activate
pip install -e . -r requirements-dev.txt     # ruff, mypy, coverage: the exact versions CI uses
```

## The checks (all must pass)

```bash
ruff check scripts tests                       # style, bugs, import order
mypy                                           # configured in pyproject.toml
coverage run -m unittest discover -s tests && coverage combine -q && coverage report   # fails below 88%
python -W error::DeprecationWarning -W error::ResourceWarning -m unittest discover -s tests
(cd extension && npm test)                     # browser-extension unit tests, needs Node 20+
MHBM_SLOW=1 python -m unittest discover -s tests -p "test_fixture.py"   # multi-split accuracy floors (CI runs this too)
(cd tests/realbrowser && npm ci)               # once; then the tests below drive a REAL Chromium-family browser (about 25 s)
CHROMIUM_BIN=/path/to/chrome python -m unittest discover -s tests -p "test_realbrowser.py" -v   # skips without node, Playwright or a browser
MHBM_FIREFOX_DIR=/path/to/FirefoxApp python -m unittest discover -s tests -p "test_real_firefox.py" -v   # real Gecko; see tests/real_firefox/README.md
```

CI runs them on Linux, macOS and Windows across Python 3.9 to 3.13. Please run the suite on 3.9 before sending a change:
that is the version most likely to break (`str | None` in runtime code, `match`, `zip(strict=)`, `removeprefix` is fine).

Without numpy the same suite runs on the pure-Python path (`MHBM_NO_NUMPY=1` forces it even where numpy is installed);
`MHBM_WORKERS=1` keeps the cross-validation in one process, `MHBM_WORKERS=N` forces N worker processes. A function more
tangled than complexity 14 fails `ruff`: split it.

## Ground rules

- **Dry run by default.** Anything that changes the library, a file the user owns, or a browser profile previews first and
  needs `--apply`; every apply goes through `Library.op(...)` so it is undoable.
- **Pin what CI runs.** GitHub Actions are pinned to a full commit SHA with the version in a comment, and the development tools to exact
  versions (`requirements-dev.txt`); Dependabot proposes updates and `tests/test_repo_hygiene.py` fails if a pin is loosened.
  To pin a new action: `git ls-remote --tags https://github.com/OWNER/REPO` and use the *commit* SHA of a release that is a few weeks old.
- **The library file has a version.** Changing the database layout means: bump `schema.SCHEMA_VERSION`, add a step to `schema.STEPS`
  (version *n* to *n* + 1; it runs inside the upgrade transaction), and add the new tables to `schema.SCHEMA`. Opening an up-to-date library
  must not write (`tests/test_concurrency.py` holds the write lock from another process while it opens one). `tests/test_migrate.py`
  opens libraries written by the real older releases (`tests/fixtures/old-library-*.sql`, regenerated with
  `tests/fixtures/gen_old_libraries.py` from git history); add a fixture when a layout changes.
- **Never trust input.** Bookmark files, page text, plan files and MCP arguments are untrusted. Parsers raise
  `FormatError`, never a traceback; plan entries are validated before any of them is applied.
- **No real data in the repo.** Tests use synthetic bookmarks only. `tests/fixtures/gen_messy_library.py` generates the
  shared fixture; edit the generator and re-run it, never hand-edit the HTML (a test compares them).
- **Training is deterministic.** Never iterate a `set` of strings, or rely on dict order from one, where the order reaches a
  float sum or a tie-break: sort first. `tests/test_feedback.py::Determinism` trains under three hash seeds and compares.
- **Speed-ups change nothing but the time.** numpy, worker processes and caches must give the same numbers as the plain
  path; `tests/test_speed.py` and `tests/test_refactor_equivalence.py` hold the proof. Entry points that start worker
  processes need an `if __name__ == "__main__"` guard (see `mhbm/__main__.py`).
- **Tests are deterministic.** Seed every random generator; no live internet (a local HTTP server stands in, see
  `tests/common.py`); the SSRF guard is relaxed only through `netsafe.testing_allow_private()`.
- **Report honestly.** Accuracy claims come from `mhbm learn` (session-held-out cross-validation), never from training data.

## Layout

```
scripts/mhbm/            engine (cli.py is a thin dispatcher; commands/ holds one module per command group)
  browsers.py gecko.py profilesafe.py   finding browser profiles, reading them, and writing into them safely (locks, atomic writes, backups)
  store.py schema.py     SQLite library (FTS index, undo journal, snapshots, locking); layout versions and the upgrade steps
  formats.py             every reader and writer
  learn.py categorize.py rules.py   filing: model, plan application, your rules
  linkcheck.py archive.py netsafe.py   network code and the SSRF guard
  sync.py nativehost.py webui.py   integrations
  mcp/                   the MCP server: wire (stdio framing), protocol, server (session, workers), registry (one entry per tool), validate (strict
                         JSON Schema), paths (file confinement), plans (propose/apply), executor (a child process per call), results, content
  mcp_server.py          compatibility shim for the old import path
extension/               Manifest V3 browser extension (talks to nativehost.py)
.claude-plugin/ .mcp.json agents/   the Claude Code plugin (this folder is the plugin root); the marketplace file is at the repository root
tests/                   unittest; tests/fixtures holds the synthetic library
```

## Adding a command

1. Write `cmd_x(args, S)` in the matching `commands/*.py` and register it in that module's `register`.
2. Use `Out` for output (so `--json` and the RTL handling work) and `_apply_plan_cmd`/`lib.op` for changes.
3. Add user-visible strings to `i18n.py` if they are part of a core flow (English is the fallback for missing keys).
4. Add a test in `tests/`; shell completion and `--help` are generated from the parser automatically.

## Releasing

Bump `mhbm.__version__`, the extension `manifest.json` and the version line in `README.md` (a test checks they agree), add a
`CHANGELOG.md` entry, run every check above.
