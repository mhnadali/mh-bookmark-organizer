# Phase 3 hand-off: from the cloud session to a local one

Written on 2026-10-16 when the owner asked to continue **locally** and stop working in the cloud session. Everything the cloud session
produced that is worth keeping is in this directory; the rest of the repository is the normal project.

Standing rules from the owner (carry them over): talk to the owner in **Persian**, keep code, commands and file names in English; **no real
user data in the repository** (tests use synthetic bookmarks only); `LICENSE` is **the owner's decision**, never invent one; push to
the working branch only and **do not open a pull request** unless asked; destructive commands stay dry-run by default and undoable.

## 1. Where things stand

| | |
|---|---|
| Repository / branch | `mhnadali/mh-bookmark-organizer`, branch `claude/elegant-cerf-fcd4t9` |
| Version | `1.5.2` (`mhbm.__version__`, extension `manifest.json`, README, CHANGELOG agree; a test checks it) |
| Phases 0, 1, 2 | done and pushed (SQLite WAL/locks, versioned schema, DNS-rebinding defence, pinned Actions, SECURITY.md; feedback + triage; speed) |
| Phase 3 | **scouting finished, implementation not started**, except the 1.5.2 hotfix below |
| Tests | 374 pass on Python 3.13 (`-W error::DeprecationWarning -W error::ResourceWarning`); ruff and mypy clean; coverage 92 % (floor 88 %) |
| Never run | GitHub Actions on this repository (pins and Node 24 are untested), macOS, Windows, real Chrome/Edge profiles |

Already shipped from Phase 3 (1.5.2): `mhbm mcp` could not start at all (`from . import mcp_server`; fixed to `from ..`), and the old
tool layer now validates arguments, requires `apply` to be exactly `true`, takes a browser *name* instead of a path in `bm_import_browser`,
and survives malformed messages. Details and the remaining gaps: `mcp-audit.md`.

## 2. Continue locally

```bash
git clone https://github.com/mhnadali/mh-bookmark-organizer && cd mh-bookmark-organizer
git checkout claude/elegant-cerf-fcd4t9
cd .claude/skills/mh-bookmark-organizer
python3 -m venv .venv && . .venv/bin/activate
pip install -e . -r requirements-dev.txt          # add '.[fast]' for numpy
ruff check scripts tests && mypy && python -m unittest discover -s tests
```

Then start Claude Code in the repository root and say, for example: *"Read docs/phase3/HANDOFF.md and continue Phase 3 from step 1."*

What a local session can do that the cloud sandbox could not (use them):

- test against the **real** Chrome, Edge, Brave and Firefox installed on the machine (always on a **copy** of a profile or a scratch
  `--user-data-dir`, never the live profile); the cloud only had Chromium, Brave, ungoogled-chromium and a conda-forge Firefox 157;
- macOS and Windows behaviour (paths, locks, `py -3`, long paths);
- unrestricted network: link checking and archiving on real sites, and the still-unmeasured effect of archived page text on filing accuracy
  (`mhbm learn --text-effect`);
- the logged-in `claude` CLI, `claude plugin validate --strict`, `claude mcp list`, and `gh run` to watch the first real CI run.

## 3. What is in this directory

Everything under here is **reference material, not shipped code**: nothing is imported by the package, built into the wheel, or run by
`unittest discover` (the suite only collects `tests/`). Reports are the output of automated scouting agents: treat them as leads and
re-verify before relying on a claim. `scout-reports.md` has the four finished reports verbatim (findings, verified / not verified).

| Path | What it is | Apply / use |
|---|---|---|
| `mcp-audit.md` | MCP server audit (compliance, 13 security findings S-xx, redesign §3, test plan §3.13, work breakdown §3.15) | read first for MCP |
| `mcp/TOOLS.md`, `mcp/tools.json` | complete tool surface: 44 tools, propose/apply split with `preview_id`, resources, prompts, risk register, 20 CLI/store prerequisites | spec for the new `bm_*` tools |
| `mcp/proto.diff`, `mcp/proto_tests/` | working prototype of the redesigned server (`mhbm/mcp/` package, 62 tests, golden transcripts for 3 protocol revisions) | diff base is `994f060`; applies to HEAD except `mcp_server.py`: `git apply -p1 --directory=.claude/skills/mh-bookmark-organizer/scripts --exclude='*mhbm/mcp_server.py' docs/phase3/mcp/proto.diff` |
| `mcp/tools-reference/`, `mcp/audit-harness/` | reference server + validators + scenario tests; the scripts that drove the old server | for comparison and test ideas |
| `real-chromium/patches/all.diff` | fixes for 9 defects found with real Chromium (3 are silent data loss) | `git apply -p1 --directory=.claude/skills/mh-bookmark-organizer docs/phase3/real-chromium/patches/all.diff` (checks clean on HEAD) |
| `real-chromium/commit/tests/` | `test_chromium_guards.py` (17), `test_realbrowser.py` (11) + `realbrowser/` Node driver, probe extension, lockfile | copy into `tests/`; real-browser tests skip without Node, Playwright and a browser; `CHROMIUM_BIN` picks any Chromium-family binary |
| `real-chromium/experiments/`, `README.md` | the experiments behind the findings | |
| `real-firefox/patches/firefox-gecko-code.patch`, `…-docs.patch` | fixes for 13 defects found with real Gecko (2 high) + 25 offline tests + live tests | same `git apply` form; each checks clean on HEAD alone, but **they conflict with the Chromium patch in `browsers.py` (one hunk of eight)**: apply one, merge the other by hand |
| `real-firefox/fixtures/` | real Firefox 157 `places.sqlite.gz` + `.jsonlz4` + expected tree, written by Firefox from a synthetic bookmarks file (`PROVENANCE.txt`) | already referenced by the patch's tests |
| `real-firefox/harness/`, `ci/`, `evidence/` | stdlib Marionette driver, netns runner, CI job pinning the conda-forge Firefox by sha256, raw logs | optional real-Firefox tests |
| `packaging/` | **partial** (scout cut off): `release.yml` draft (380 lines: verify, reproducible build, sdist tests, smoke matrix, attestations, TestPyPI then PyPI by OIDC), `smoke_installed.py` (11 install-and-use checks; the logs show 11/11 for the wheel, the wheel with `[fast]` and the sdist on Python 3.9, 3.10, 3.11, 3.12 and 3.13, built from the 1.5.2 tree), `run_matrix.sh`, `normalize_sdist.py`, `linkscan.py` | adapt, then re-resolve every action SHA yourself |
| `plugin/` | **almost nothing finished** (scout cut off): two layout experiments (`planA`, `planB` manifests), `gitserve.py`, `lib.sh`, `probe_init.py` | start over from the official docs, see step 6 |
| `profiles-audit/` | **partial**: `test_profiles.py` (1028 lines, fake-home discovery tests), `exp/exp01..04` scripts with outputs (discovery baseline, write path, backup collision, hot WAL copy); no patches were written | the outputs are the bug list, see step 3 |

## 4. Work plan, in the order I would do it

Each step ends with ruff, mypy and the full suite green; commit per step; push to the branch.

1. **Real-Chromium fixes** (`real-chromium/`). Apply `all.diff`, copy the two test files and `realbrowser/` into `tests/`, run `npm ci` in
   `tests/realbrowser`, run the 11 real-browser tests against the machine's real Chrome (`CHROMIUM_BIN=/path/to/chrome`). The report's list:
   unparseable URLs are silently dropped by the browser and then deleted from the library by the next `sync --apply`; folders without a
   root name never match, so every sync deletes and re-adds them; URLs the browser re-encodes get a different sync key (churn plus deletions);
   a running browser is not detected; `sync` loosens `0600` to `0644` and `install-chrome` writes non-atomically; `verify-chrome` misses bad
   versions/ids and crashes on some nodes; 98+ nested folders make Chromium discard the file; export order is alphabetical. The checksum
   algorithm itself matched Chromium byte for byte.
2. **Real-Firefox fixes** (`real-firefox/`). Apply the code and docs patches, merge the one conflicting hunk with step 1. High: a fresh
   Linux Firefox (>=147) keeps its profile in `~/.config/mozilla/firefox` and `mhbm browsers` finds nothing; with several profiles the wrong
   one is read (`installs.ini` ignored). Also: a db/WAL copy that straddles a checkpoint, backup root named `unfiled`, `place:` queries
   imported as bookmarks, an LZ4 length bomb, forks (Zen, Floorp, LibreWolf, Waterfox) not offered. Add them to the MCP browser-name list.
3. **Profile discovery and safety audit** (`profiles-audit/`). Re-run `exp01..04`, turn the results into patches: Chrome `AccountBookmarks`,
   Edge/Opera/Vivaldi/Brave/Arc layouts, Snap/Flatpak/WSL, a reliable "browser is running" check for `install-chrome`/`sync`, backup before
   writing, atomic write with fsync, permissions. Finish `test_profiles.py` (deterministic, fake homes) and commit it.
4. **Security fixes that are independent of the MCP rewrite** (found by the audit; each needs a test):
   - `Library.op()` is not one transaction: an interrupted `apply-plan --apply` left 14,400-18,400 of 20,000 rows in 5 of 18 runs (SIGTERM
     and SIGKILL were not even journaled). Prototype change "C7" is in `proto.diff`; this affects every front end, not just MCP.
   - `config` prints `notify` values from `config.json` verbatim; `title_regex` rules allow ReDoS; `check`/`archive` are not journaled.
   - Behind an HTTP proxy, `127.1`, `0177.0.0.1` and `0x7f.0.0.1` are not recognised as literal IPs by `netsafe._literal_ip` (my Phase 0
     code), so they are forwarded to the proxy; normalise with `socket.inet_aton` and add tests. Verify first.
   - `organize`, `triage` and `restructure` raise `IndexError` in `learn.fit_logistic` on a 20,000-bookmark library with no learnable structure.
5. **MCP redesign (1.6.0)**: implement `mhbm/mcp/` from `mcp-audit.md` §3 and `mcp/TOOLS.md`, starting from `proto.diff`. Acceptance (audit §3.15):
   a subprocess handshake test on all OSes, golden transcripts for three protocol revisions, validator differential test, `claude mcp list`
   shows *Connected*, no stdout writes outside the wire, `--read-only` really read-only. 1.6.0 must say plainly that tool names and the
   `apply` flags change. Owner decisions needed first: breaking tool names; default allow-dir (`<home>/exchange` instead of the cwd);
   whether `bm_apply`, `bm_triage_decide` and `bm_undo` carry `_meta anthropic/requiresUserInteraction`; whether the plugin's MCP server is
   read-only by default.
6. **Claude Code plugin + marketplace.** Docs are reachable with `curl -sL https://code.claude.com/docs/en/plugins-reference` (also
   `.../plugin-marketplaces`, `.../mcp`, `.../hooks`, `.../env-vars`). `claude plugin validate --strict` and an install into an isolated
   `HOME`/`CLAUDE_CONFIG_DIR` are the acceptance tests. Open design questions: how to avoid duplicating the skill (symlink vs generated copy
   with a drift test vs a plugin root inside the skill directory), where the library lives (never inside the plugin cache; look for the
   persistent plugin data variable), Python discovery on Windows, namespaced tool names. Do not add a `license` field until the owner picks one.
7. **PyPI package.** Finish `pyproject.toml` (classifiers, URLs, `py.typed`, extension and skill files for pip users), adapt
   `packaging/release.yml`, add a `package` job to `test.yml` and `tests/test_packaging.py`, re-resolve every action SHA. The name
   `mh-bookmark-organizer` was not on PyPI on 2026-10-15 (`pip index versions` found nothing; check again). **Blockers only the owner can
   clear:** a `LICENSE` file, and a PyPI/TestPyPI "pending publisher" (project, owner, repo, workflow file, environment names).
8. **Adversarial review of the whole Phase 3 diff** (security, MCP conformance, packaging, plugin, tests, docs), fix what is confirmed.
9. **Release 1.6.0**: CHANGELOG, README/SKILL/agent docs, Python 3.9-3.13 matrix with and without numpy, coverage floor, privacy scan,
   push. Watch the first GitHub Actions run and fix what only CI can show.

## 5. Not verified by anyone yet

GitHub Actions on this repository; macOS and Windows; real Chrome Sync / `AccountBookmarks`; Edge, Vivaldi, Opera; Snap, Flatpak, LibreWolf
and Waterfox layouts; Claude Code's behaviour with the new MCP metadata (only its documentation was read); link checking and archiving on
real sites; the effect of archived page text on filing accuracy; the 60 s propose target for a 45,000-bookmark library (76 s measured);
the translations (ar, fa, es, fr, de) have not been reviewed by a professional.
