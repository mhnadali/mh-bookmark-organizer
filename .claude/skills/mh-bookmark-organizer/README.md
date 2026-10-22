# mh-bookmark-organizer

A Claude Code agent + skill (and a standalone CLI, MCP server, browser extension and local web UI) that turns a messy browser
bookmark collection into a clean, tagged, searchable library. Pure Python standard library, Python 3.9+; only link
checking and page archiving use the network. Version 1.5.2 - see [CHANGELOG.md](https://github.com/mhnadali/mh-bookmark-organizer/blob/main/.claude/skills/mh-bookmark-organizer/CHANGELOG.md).

## Capabilities

| Capability | Inspired by | Commands |
|------------|-------------|----------|
| Import Chrome/Brave/Edge/Vivaldi/Opera/Firefox/Safari profiles directly, plus HTML, CSV, JSON, Pocket, Raindrop, Instapaper, Omnivore, Karakeep, Linkwarden | Karakeep, Buku, linkding, Shiori | `import`, `import-browser`, `browsers` |
| Export HTML (folder dates, icons, empty folders kept), Chrome JSON, CSV, JSON, Markdown, Obsidian, Raindrop, Pocket, Linkwarden, Karakeep, Instapaper | Shiori, Buku | `export` |
| Two-way sync with a Chromium `Bookmarks` file (patched in place, checksum verified) | Floccus | `sync`, `install-chrome`, `verify-chrome` |
| Exact, variant (mobile/AMP/canonical) and fuzzy duplicate detection; interactive review | linkding, Floccus | `dedupe`, `clean-urls`, `canonical` |
| Careful dead-link detection: retries, per-host throttling, multi-run confirmation, Wayback copies, safe 301 rewrite | Linkwarden, Floccus | `check`, `prune` |
| Sanitised single-file page snapshots (scripts removed, strict CSP), PDF text, robots.txt aware | Linkwarden, Shiori | `archive` |
| Full-text search with operators (`tag:` `folder:` `site:` `status:` `is:` `before:` `after:`, phrases, `-exclude`); Persian/Arabic letter variants match | Linkwarden, Karakeep | `search` |
| Filing and tagging **learned from your own folders** (calibrated model), your own rules, taxonomy rules, then the agent for the rest; `learn --seeds/--curve/--text-effect` measure it | Karakeep | `learn`, `organize`, `rules`, `ai-batch`, `apply-plan` |
| Decide in groups: unfiled bookmarks grouped by site / saved-together, one decision per group, interactive or as a reviewable plan | - | `triage` |
| Learns from what you do: accepted / corrected / rejected suggestions are recorded, rejected folders are never offered again, the stated confidence is checked against reality | - | `feedback` |
| Tag hygiene and suggestions, title clean-up, tag/folder synonyms | Karakeep, linkding | `tags`, `autotag`, `titles`, `synonyms` |
| Folder-tree tuning: split bloated, fold tiny, merge look-alike folders (flattening is opt-in) | - | `restructure`, `folders` |
| Reading list: unread/reading/read, favourites | Pocket, Raindrop | `mark`, `reading-list` |
| Reviewable plan files (offline review page), undo journal, snapshots, dry-run by default | Floccus | `review`, `history`, `undo`, `backups` |
| Guided clean-up and self-diagnosis | - | `wizard`, `doctor` |
| Offline HTML dashboard, local web UI | - | `dashboard`, `ui` |
| Weekly non-destructive upkeep with desktop/e-mail/webhook summaries | - | `maintain`, `schedule`, `notify` |
| MCP server; browser extension with native-messaging host | - | `mcp`, `install-native-host`, `native-host` |
| `--json` on every command, config file, bash/zsh/fish completion, messages in en/fa/ar/es/fr/de | - | `--json`, `--lang`, `config`, `completion` |

## Install

Pick one:

- **Claude Code plugin** (skill + subagent + MCP server): `/plugin marketplace add mhnadali/mh-bookmark-organizer`, then
  `/plugin install mh-bookmark-organizer@mhnadali-skills` (details under *Integrations*).
- **OpenAI Codex plugin** (skill + MCP server): `codex plugin marketplace add mhnadali/mh-bookmark-organizer`, then
  `codex plugin add mh-bookmark-organizer@mhnadali-skills` (details under *Integrations*).
- **Copy** `.claude/agents/mh-bookmark-organizer.md` and `.claude/skills/mh-bookmark-organizer/` into your project's (or `~/.claude/`) directory.
- **Python package** (the engine and the commands `mhbm` and `mhbm-mcp`, nothing else): `pip install .` inside the skill directory, or
  `pip install mh-bookmark-organizer` once it is published (the release is prepared; it waits for the owner to choose a licence, see
  [RELEASING.md](https://github.com/mhnadali/mh-bookmark-organizer/blob/main/.claude/skills/mh-bookmark-organizer/RELEASING.md)). The wheel does not contain the browser extension or the skill; the sdist does.

SQLite with FTS5 gives ranked search; without it search falls back to plain matching. Run `mhbm doctor` to check your setup.

## Quick start

```bash
cd .claude/skills/mh-bookmark-organizer/scripts
export MHBM_HOME=~/bm-library
python3 -m mhbm import ~/Downloads/bookmarks.html      # or: import-browser chrome
python3 -m mhbm stats
python3 -m mhbm dedupe --apply
python3 -m mhbm check && python3 -m mhbm archive --limit 50
python3 -m mhbm learn                                  # how well your folders can be learned
python3 -m mhbm organize --plan-out plan.json          # review, then: apply-plan plan.json --apply
python3 -m mhbm export --format html --out ~/organized.html   # import this in your browser
python3 -m mhbm dashboard
```

New to it? `python3 -m mhbm wizard` walks through the same steps, previewing each and asking before it applies anything.
Or ask Claude: "use mh-bookmark-organizer to clean up ~/Downloads/bookmarks.html".

## How accurate is the filing?

`organize` files only bookmarks that are still unfiled, using what you already filed. `mhbm learn` measures how well that works on
*your* library by holding out whole saving sessions (so a bookmark is never judged by neighbours saved in the same sitting),
and prints a table of how many bookmarks get an answer at each confidence target and how often those answers are right.
`learn --seeds 3` repeats it on different splits and shows the spread.

How to read the result honestly:

- The default `--min-confidence 0.85` means "answer only when at least 85% likely to be right *on your library*", measured
  out-of-sample. Lower it for more suggestions and more mistakes.
- In a very granular tree (hundreds of folders, many with only one or two bookmarks) the exact folder often cannot be known
  from a title and URL. The model then answers one or two levels up ("sure of the area, not the sub-folder") instead of guessing.
- On the author's own library (1,422 filed bookmarks in 171 folders, up to 10 levels deep, a quarter of the titles in Persian),
  `learn --seeds 3` reports: the exact folder is the top suggestion for 39% of held-out bookmarks (39-40% across splits) and
  among the top three for 53%. Answering only when 80% sure it covers 26% of bookmarks at 87% precision (90%: 17% at 92%;
  70%: 33% at 83%), so at the default threshold roughly one bookmark in five is filed automatically and about nine of ten
  of those are right; the rest go to the agent (`ai-batch`), to `triage`, or stay for you. Part of the gap cannot be closed
  by any model that learns from examples: 18% of that library's bookmarks sit in a folder with no example from another sitting.
- `learn --curve` shows the data is nearly used up: learning from a quarter, half, three quarters and all of those bookmarks gives
  a best guess right 26%, 35%, 38% and 40% of the time. Filing more of the same kind of bookmark helps less and less; new
  information (archived page text, your decisions through `feedback`) is what can still move it. Run `archive` and then
  `learn --text-effect` to see what the page text adds on your data: that could not be measured offline here.
- Bookmarks saved together are judged together (`top1_pooled`: 42% instead of 39% on that library). `triage` uses that: it
  groups unfiled bookmarks (same site and the same best guess, saved together) and a simulated user needed about a third to
  40% fewer decisions than going one by one. Groups whose suggestion has real support (30%+) were right for about 71% of their
  members on held-out sittings; weaker groups are not worth trusting, and are listed after the confident ones.
- Training is deterministic: the same library gives the same suggestions every time (no dependence on the Python process).
- The repository's tests use a synthetic library (`tests/fixtures`) whose vocabulary is far cleaner than real life, so its
  scores are much higher; those tests guard against regressions and are not an accuracy claim.

## Speed

Training the filing model happens once and is cached (`model.json`) until your filed bookmarks change. Measured on the
author's machine (4 cores), same numbers every time:

| Library | pure Python, 1 process | numpy, 1 process | numpy, 4 worker processes |
|---------|------------------------|------------------|---------------------------|
| 1,422 filed bookmarks (real) | 41 s (was 47 s) | 14 s | **9 s** |
| 45,000 filed bookmarks (synthetic, 2,000 folders) | - | - | 129 s |

`pip install numpy` (or `pip install .[fast]`) is optional and changes only the time: the numpy and pure-Python paths agree to
about 1e-15 and `doctor` says which is in use. Libraries above about 600 filed bookmarks use one worker process per core
(`MHBM_WORKERS=1` turns that off); above about 6,000 only a sample is judged out-of-sample. Answering for one bookmark takes
about 10 ms on a 10,000-bookmark library and 15 ms on a 45,000-bookmark one. `restructure` on 45,000 bookmarks takes
under a second (it took 16 s), and `triage` groups 20,000 unfiled bookmarks in about a second.

## Integrations

**Claude Code plugin** (skill + subagent + MCP server in one install)

```
/plugin marketplace add mhnadali/mh-bookmark-organizer
/plugin install mh-bookmark-organizer@mhnadali-skills
```

(`/plugin install mh-bookmark-organizer --marketplace mhnadali/mh-bookmark-organizer` does both in one step on Claude Code 2.1.275 or later.) Enabling
the plugin asks four things: the Python command (`python3`; `py` on Windows), the **library folder** (kept outside the plugin, so updates and
uninstalling never touch it), whether the MCP server is read-only (default: no; plans are proposed first, every apply is undoable) and
whether to hide the tools that contact other sites. The MCP tools then appear as `mcp__plugin_mh-bookmark-organizer_bookmarks__bm_*`. The
plugin is the skill folder itself, so there is one copy of everything; `tests/test_plugin.py` checks the manifest, the marketplace entry and the
agent copy, and runs `claude plugin validate --strict` when the CLI is installed.

**OpenAI Codex plugin** (skill + MCP server)

```
codex plugin marketplace add mhnadali/mh-bookmark-organizer
codex plugin add mh-bookmark-organizer@mhnadali-skills
```

Codex reads the same marketplace file as Claude Code (`.claude-plugin/marketplace.json`) and the plugin's own manifest `.codex-plugin/plugin.json`.
It adds the skill `$mh-bookmark-organizer` (`codex/skills/`, written for Codex: it works through the MCP tools and follows the same safety rules) and starts
the MCP server `bookmarks`. Codex has no per-plugin settings and passes a server only a few environment variables, so the plugin uses fixed, safe
defaults: the library is `~/.mh-bookmark-organizer`, and the tools read and write files only in your `Downloads`, `Documents` and `Desktop`
folders (never in the library). To choose the folders yourself, register the server by hand instead of installing the plugin:

```bash
codex mcp add bookmarks --env MHBM_HOME=~/bm-library --env MHBM_MCP_ALLOW_DIRS=~/Downloads:~/exports \
  -- python3 /path/to/.claude/skills/mh-bookmark-organizer/scripts/codex_mcp.py        # `;` between folders on Windows; add --env MHBM_MCP_READ_ONLY=1 for look-only
```

(`~` is expanded by your shell here; Codex stores the result.) With the Python package, `codex mcp add bookmarks -- mhbm-mcp --home ~/bm-library --allow-dir ~/Downloads` is
the same server without the Codex defaults. The server needs `python3` (3.9+) on the `PATH` that Codex uses. `tests/test_codex.py` checks the manifest and the
launcher, and, when the `codex` command is installed, has the real Codex install the plugin, start the server, list its 29 tools and relay a call
(checked with Codex CLI 0.163.0; the plugin format is young, so newer versions may change details). Not verified: a model-driven session (needs a Codex login).

**MCP server** (29 tools, 6 prompts, 4 resources and 2 resource templates; protocol revisions 2024-11-05 to 2025-11-25)

```bash
claude mcp add bookmarks --scope user -e PYTHONPATH=/path/to/.claude/skills/mh-bookmark-organizer/scripts \
  -- python3 -m mhbm.mcp --home /path/to/library --read-only          # after `pip install mh-bookmark-organizer`: `-- mhbm-mcp --home ...`
```

Other clients: `{"mcpServers": {"bookmarks": {"command": "python3", "args": ["-m", "mhbm.mcp", "--home", "/path/to/library"], "env": {"PYTHONPATH": "..."}}}}`.
`mhbm mcp` is the same server. Options: `--read-only`, `--no-network`, `--allow-dir DIR` (repeatable), `--disable-tool NAME`,
`--max-result-chars N`, `--no-client-roots`. The default home is `$MHBM_HOME`, else `./.mhbm`.

*Look, then propose, then apply.* Read-only tools: `bm_stats bm_search bm_folders bm_tags bm_history bm_reading_list bm_triage
bm_feedback bm_rules bm_browsers bm_backups bm_doctor bm_learn`. Plan tools: `bm_organize bm_dedupe bm_restructure bm_autotag bm_titles
bm_prune` change **nothing**: each returns a `plan_id` and a sample. `bm_apply_plan(plan_id)` applies exactly that plan, once, only while
the library is unchanged since (a stale plan is refused), and every apply is snapshotted and undoable with `bm_undo`. Direct edits:
`bm_tag bm_mark bm_rules_edit bm_import_file bm_import_browser bm_export`. Network: `bm_check_links`, `bm_archive` (they contact the
bookmarked sites). Prompts (slash commands in Claude Code): `tidy-unfiled clean-dead-links merge-duplicates weekly-review find-bookmark
restructure-folders`. Resources: `mhbm://library/summary|folders|history|rules`, `mhbm://bookmark/{id}`, `mhbm://plan/{plan_id}`.

Safety: the server is pinned to `--home`; with `--read-only` only the read tools exist and the library is opened read-only (nothing on
disk changes); the file tools reach only the allowed directories (`--allow-dir`, else the client's roots, else `$CLAUDE_PROJECT_DIR`, else
the working directory), open files without following links, never overwrite, and never touch the library folder; every argument is
validated against a strict schema (no coercion: `"false"` is not `false`); each call runs in its own process with a time limit,
cancellation and a minimal environment; results are size-capped and say so when they are cut. Details: [SECURITY.md](https://github.com/mhnadali/mh-bookmark-organizer/blob/main/SECURITY.md).
All bookmark imports are limited to 64 MiB and 250,000 records. Obsidian export stages a complete vault and requires a new destination directory; it refuses to overwrite an existing vault.
**Upgrading from 1.5.x:** the old server could not start (see the 1.5.2 notes), so nothing depends on its tool names; the new names and
the `plan_id` flow are listed in the [CHANGELOG](https://github.com/mhnadali/mh-bookmark-organizer/blob/main/.claude/skills/mh-bookmark-organizer/CHANGELOG.md). The design notes are in [docs/phase3/mcp-audit.md](https://github.com/mhnadali/mh-bookmark-organizer/blob/main/docs/phase3/mcp-audit.md).

**Browser extension** (Chrome, Edge, Brave, Vivaldi, Chromium): save a bookmark into the folder you would choose, search the
library, and apply suggested moves in the browser. See [extension/README.md](https://github.com/mhnadali/mh-bookmark-organizer/blob/main/.claude/skills/mh-bookmark-organizer/extension/README.md).

**Local web UI**: `mhbm ui` serves a browser UI on 127.0.0.1 only, with a random per-run token in the URL fragment.

**Notifications**: `mhbm maintain --notify` summarises scheduled upkeep by desktop notification, e-mail or webhook
(configure `notify` in `config.json`; secrets come from `MHBM_SMTP_PASSWORD` / `MHBM_WEBHOOK_URL`, never the file).

## Safety and security notes

- Destructive commands are dry runs unless `--apply`; every apply snapshots and is undoable, and is all-or-nothing: an interruption halfway leaves the library as it was. Link checking and archiving only record what they observed and are not in the undo journal. Browser profiles are never written without `--confirm-chrome-closed`.
- Network fetches only reach public http(s) hosts: every redirect is re-validated, and the address that was checked is the address that is connected to, so a DNS answer that changes in between cannot send a request to a private address. Through an HTTP proxy the proxy resolves names and applies its own policy. It is a strong guard, not a sandbox. See [SECURITY.md](https://github.com/mhnadali/mh-bookmark-organizer/blob/main/SECURITY.md) for the threat model and how to report a problem.
- Several mhbm processes (a scheduled `maintain`, the web UI, the extension's native host, your shell) can share one library: it uses write-ahead logging and waits for a busy lock. A library file written by an older release is upgraded in one transaction after a copy is saved in `backups/`; one written by a *newer* release is refused rather than damaged.
- Archived pages are stored as sanitised single files (no scripts, iframes or event handlers, `Content-Security-Policy` blocks remote loads). Treat them as untrusted anyway.
- CSV export neutralises spreadsheet formula injection; Markdown/Obsidian export escapes titles and URLs and keeps files inside the target directory.
- Plan files and bookmark files are untrusted input: malformed ones are rejected with a message, and a rejected plan entry changes nothing.
- Links count as dead only after repeated failures over time; sites that block bots are `restricted`, never dead.

## Known limits and what has been verified

- Archiving fetches static HTML only; pages that render with JavaScript yield little text. PDF text needs poppler's `pdftotext`.
- The learned model needs roughly 10+ filed bookmarks in 2+ folders; below about 150 it uses a fixed, uncalibrated blend.
- Verified: the full suite (about 370 tests) on Linux under Python 3.9, 3.10, 3.11, 3.12 and 3.13, with and without numpy; a real 1,500-bookmark Chrome
  export (import, dedupe, clean-urls, organize, restructure, undo, HTML export); 50,000 synthetic bookmarks; the extension
  and native host end-to-end in headless Chromium; Firefox 157, Zen 1.23 and Floorp 12.20 (real Gecko, headless, no network) writing
  profiles that `import-browser` reads back exactly - `places.sqlite`, `bookmarkbackups/*.jsonlz4`, a killed browser's WAL, a browser that is
  running - and importing our HTML export (`tests/real_firefox/`, run with `MHBM_FIREFOX_DIR`); link checking and archiving against a local test server and a proxied sandbox.
- **Not verified** (be careful, and keep the backups the tool makes): link checks and archiving against many real sites;
  macOS and Windows (the CI matrix covers them but had not been run when this was written); real Brave/Edge/Vivaldi/Safari
  profiles, and Firefox on macOS/Windows/Snap/Flatpak and LibreWolf/Waterfox (their folder layouts are from documentation, not from a run); writing a Chromium `Bookmarks` file into a real Chrome profile with Chrome Sync, `AccountBookmarks` or on Windows/macOS
  (the file we write is checked against a real headless Chromium 141 on scratch profiles - ids, GUIDs, checksum, order, dates and the
  browser's own rewrite - by `tests/test_realbrowser.py`, which needs Node and a browser and skips without them); real zsh/fish
  completion; the Arabic, Persian, Spanish, French and German translations are not professionally reviewed.

## Development

See [CONTRIBUTING.md](https://github.com/mhnadali/mh-bookmark-organizer/blob/main/.claude/skills/mh-bookmark-organizer/CONTRIBUTING.md). In short: `ruff check scripts tests`, `mypy`, and
`python -m unittest discover -s tests` from this directory. Problems? [TROUBLESHOOTING.md](https://github.com/mhnadali/mh-bookmark-organizer/blob/main/.claude/skills/mh-bookmark-organizer/TROUBLESHOOTING.md).
