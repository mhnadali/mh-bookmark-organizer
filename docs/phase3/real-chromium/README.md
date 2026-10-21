# harness/ - what is in here and how to run it

```
commit/                     files meant to be committed to the repo (paths are relative to .claude/skills/mh-bookmark-organizer/)
  tests/test_realbrowser.py       11 tests that drive a real browser (skip cleanly when none is available)
  tests/test_chromium_guards.py   17 pure-Python tests of the same guards (run everywhere, 0.03 s)
  tests/realbrowser/driver.js     one-command-per-process Node driver (Playwright persistent context + probe extension)
  tests/realbrowser/probe-ext/    the MV3 probe extension (permission "bookmarks" only; no code but a comment)
  tests/realbrowser/package.json  pins playwright-core 1.56.1 (= Chromium 141.0.7390.37, build 1194)
  tests/realbrowser/package-lock.json
patches/                    unified diffs against scripts/mhbm/ (apply with `patch -p1` from the skill directory)
  all.diff                        everything below in one file
  urlnorm.diff  formats.diff  sync.diff  commands_data_io.diff  browsers.diff
experiments/                the exploratory scripts that produced REPORT.md (not for committing; they take absolute paths)
```

## Run the committed tests

```
cd .claude/skills/mh-bookmark-organizer
python -m unittest discover -s tests -p "test_chromium_guards.py" -v      # no browser needed
python -m unittest discover -s tests -p "test_realbrowser.py" -v          # skips unless node + playwright + a browser exist
CHROMIUM_BIN=/path/to/brave python -m unittest discover -s tests -p "test_realbrowser.py" -v   # any Chromium-family binary
```

The driver looks for Playwright in this order: `$PLAYWRIGHT_MODULE`, `playwright-core`, `playwright`, `@playwright/test`,
`/opt/node22/lib/node_modules/playwright` (this sandbox).  It looks for a browser in this order: `--bin`, `$CHROMIUM_BIN`,
Playwright's own `executablePath()`, `$PLAYWRIGHT_BROWSERS_PATH/chromium-*/chrome-linux/chrome`.
`node tests/realbrowser/driver.js probe` prints what it found (exit 3 if nothing).

In CI: `npm ci` in `tests/realbrowser` (8.7 MB, 1.4 s here) and `npx playwright-core install --with-deps chromium`
(this downloads ~170 MB from Playwright's CDN, which this sandbox blocks, so that step is unverified).

## Re-run the experiments (all use scratch profiles under a directory you choose)

```
cd experiments
node exp_a_create.js <udd> spec_a.json <tree.json>              # A: browser builds a tree (Persian, nested, duplicates, 15 kB URL, javascript:, data:)
./exp_b_check.sh <workdir> <skill_dir> <library_home> [bin]     # B1: install-chrome -> browser -> compare -> browser rewrites -> verify
python3 exp_c_cases.py <skill> <base.json> <skeleton_udd> <workdir> [case ...] [--bin B]   # C: 51 corrupted files
python3 exp_b3_hostile.py <skill> <workdir> <skeleton_udd>      # B3: hostile titles/URLs/folder names
python3 exp_b4_dates_roots.py <skill> <workdir> <skeleton_udd>  # B4: dates 1970..year 10000, Mobile bookmarks root
python3 exp_urlvalid.py <skill> <skeleton_udd> <workdir> [--bin B]   # which url strings the browser drops (MHBM_CANDIDATES=file.json for another list)
python3 fuzz_urls.py 4000 99 > urls.json; node exp_canon.js <udd> urls.json canon.json [--bin B]; python3 check_canon.py <skill> canon.json --proposed proposed/browser_key.py
python3 exp_depth.py <skill> <skeleton_udd> <workdir> 80 130     # folder nesting limit
./bisect_checksum.sh <brave_version> <skill> <root>              # download a Brave release, probe checksum handling, delete it
```

`<skeleton_udd>` is any user-data-dir the browser has started in once (it makes runs faster and identical); `<udd>` is a new dir.
Every script uses throw-away profiles and never touches a real one.  Do not `pkill chrome` on a shared machine: other jobs run browsers too.
