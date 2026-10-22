# mh-bookmark-organizer

[![tests](https://github.com/mhnadali/mh-bookmark-organizer/actions/workflows/test.yml/badge.svg)](https://github.com/mhnadali/mh-bookmark-organizer/actions/workflows/test.yml)

Turn a messy browser bookmark collection into a clean, tagged, searchable library, with an AI assistant doing the tedious part and **you approving
every change**. It is a local Python engine (`mhbm`) with a command line, an MCP server, plugins for Claude Code and OpenAI Codex, a Chromium
extension and a local web page. It uses the Python standard library only, needs Python 3.9 or newer, and keeps your bookmarks on your machine.

*فارسی: [راهنمای فارسی](docs/README.fa.md)*

**Contents** · [At a glance](#at-a-glance) · [Install](#install) · [First session](#first-session) · [How a change happens](#how-a-change-happens) · [What it can do](#what-it-can-do) ·
[MCP reference](#mcp-reference) · [How accurate is the filing?](#how-accurate-is-the-filing) · [Safety and privacy](#safety-and-privacy) · [Codex notes](#openai-codex-notes) ·
[Verified and not verified](#what-has-and-has-not-been-verified) · [FAQ](#faq) · [Project status](#project-status) · [Repository layout](#repository-layout)

## At a glance

| | |
|-|-|
| **Does** | import from any browser or export file · merge duplicates · detect dead links carefully · file unfiled bookmarks into folders learned from *your* folders · restructure the folder tree · tag, clean titles, keep a reading list · archive pages for full-text search · export back to a browser |
| **Interfaces** | `mhbm` command line · MCP server (29 tools, 6 prompts, 4 resources + 2 resource templates) · Claude Code plugin (skill + subagent + MCP) · OpenAI Codex plugin (skill + MCP) · Chromium extension · local web UI (`mhbm ui`) |
| **Requires** | Python 3.9+ and nothing else. Optional: `numpy` (faster model training, same results), `pdftotext` (PDF text), an SQLite with FTS5 (ranked search; plain matching without it) |
| **Where your data lives** | one folder you choose (`--home`, `$MHBM_HOME`, else `./.mhbm`): `library.db` (SQLite), `snapshots/`, `archive/`, `rules.json`, `model.json` |
| **Network** | only link checking and page archiving, only to public `http(s)` addresses, only when you ask. No telemetry, no account, no cloud service |
| **Language of messages** | English, Persian, Arabic, Spanish, French, German (`--lang`; the last four are not professionally reviewed) |
| **Version** | 1.5.2 in the repository; 1.6.0 (rebuilt MCP server, plugins, PyPI package) is prepared, not released |
| **Licence** | none chosen yet (see [Project status](#project-status)) |

## Install

Python 3.9+ must be on `PATH`: the Codex plugin uses `python` (also supplied by the Windows Python launcher setup); manual commands use `python3` on macOS/Linux or `py -3`/`python` on Windows.
### Claude Code

```
/plugin marketplace add mhnadali/mh-bookmark-organizer
/plugin install mh-bookmark-organizer@mhnadali-skills
```

Enabling the plugin asks four things: the Python command, the **library folder** (kept outside the plugin, so updates and uninstalling never touch it),
whether the MCP server is read-only, and whether to hide the tools that contact other sites. You get the skill, the `mh-bookmark-organizer` subagent and
the MCP tools (`mcp__plugin_mh-bookmark-organizer_bookmarks__bm_*`), plus six prompts you can run as slash commands.

### OpenAI Codex

```
codex plugin marketplace add mhnadali/mh-bookmark-organizer
codex plugin add mh-bookmark-organizer@mhnadali-skills
```

You get the skill `$mh-bookmark-organizer` and the MCP server `bookmarks`. Details, defaults and the manual alternative: [Codex notes](#openai-codex-notes).

### Any other MCP client

Without installing anything (the engine is plain Python files):

```json
{"mcpServers": {"bookmarks": {"command": "python3", "args": ["-m", "mhbm.mcp", "--home", "/path/to/library", "--allow-dir", "/path/to/your/exports"],
  "env": {"PYTHONPATH": "/path/to/mh-bookmark-organizer/.claude/skills/mh-bookmark-organizer/scripts"}}}}
```

On Windows, use `"command": "python"` for the equivalent configuration. After `pip install .` in `.claude/skills/mh-bookmark-organizer`, `"command": "mhbm-mcp"` works on any platform with the same arguments and no `env`. Add `--read-only` if the client should
only look, `--no-network` to hide the tools that contact other sites. (With `python3 -m`, the client's working folder is searched first for the module `mhbm`, so do not start the server
from a folder that contains one; the plugins avoid this by running a script instead.)

### Command line only

```bash
git clone https://github.com/mhnadali/mh-bookmark-organizer && cd mh-bookmark-organizer/.claude/skills/mh-bookmark-organizer
pip install .                       # gives the commands `mhbm` and `mhbm-mcp`; or run `python3 -m mhbm` from the scripts/ folder without installing
mhbm doctor                         # checks Python, SQLite, the library, proxy and DNS, and says what to fix
```

A PyPI release (`pip install mh-bookmark-organizer`) is prepared but not published.

## First session

Export your bookmarks from the browser first (Chrome: *Bookmarks → Bookmark manager → ⋮ → Export bookmarks*), or let the tool read the profile (it copies the
file; it never opens the live one for writing). Then say, in Claude Code or Codex:

> use mh-bookmark-organizer to clean up ~/Downloads/bookmarks.html

The assistant imports the file, shows a health report (how many are unfiled, duplicated, dead), and proposes one clean-up at a time. The same steps by hand:

```bash
export MHBM_HOME=~/bm-library
mhbm import ~/Downloads/bookmarks.html          # or: mhbm import-browser chrome
mhbm stats                                      # unfiled, duplicates, link status, empty folders, top domains and tags
mhbm dedupe                                     # preview; add --apply to merge
mhbm check && mhbm archive --limit 50           # find dead links (repeated failures over time), keep searchable copies
mhbm learn                                      # how well can your own folders be learned? (see "How accurate")
mhbm organize --plan-out plan.json              # propose; review the plan, then:  mhbm apply-plan plan.json --apply
mhbm restructure                                # preview folder-tree changes
mhbm export --format html --out ~/organized.html    # import this file back into your browser
```

`mhbm wizard` walks through the same steps, previewing each and asking before it applies anything.

## How a change happens

1. **Read-only first.** Reports and searches change nothing.
2. **Propose.** Anything that changes many bookmarks is a dry run (CLI) or a *plan tool* (MCP: `bm_organize`, `bm_dedupe`, `bm_restructure`, `bm_autotag`,
   `bm_titles`, `bm_prune`). A plan tool writes nothing: it returns a `plan_id` and a sample.
3. **Review.** Trim the plan if you like (`mhbm review plan.json` builds an offline page to tick items one by one).
4. **Apply.** `bm_apply_plan(plan_id)` / `--apply` applies exactly that plan, once, in a single transaction, and only if the library has not changed since
   the plan was made. A snapshot and an undo entry are written first.
5. **Undo.** `bm_undo` / `mhbm undo` reverts it. `mhbm backups` lists the snapshots.

Judgement calls stay with you: look-alike (fuzzy) duplicates are never merged automatically; a link is called dead only after repeated failures over
time (`suspect` means "check again", sites that block bots are `restricted`, never dead); a suggestion you reject is not offered again.

## What it can do

| Capability | Commands |
|------------|----------|
| **Import** Chrome, Brave, Edge, Vivaldi, Opera, Opera GX, Yandex, Chromium, Firefox, LibreWolf, Waterfox, Floorp, Zen and Safari profiles directly; HTML, Chrome/Firefox JSON, CSV, JSON, Pocket, Raindrop, Instapaper, Omnivore, Karakeep, Linkwarden files | `import`, `import-browser`, `browsers` |
| **Export** HTML (folder dates, icons, empty folders kept), Chrome JSON, CSV, JSON, Markdown, Obsidian, Raindrop, Pocket, Linkwarden, Karakeep, Instapaper | `export` |
| **Two-way sync** with a Chromium `Bookmarks` file while the browser is closed (patched in place, checksum verified, backup kept) | `sync`, `install-chrome`, `verify-chrome` |
| **Duplicates**: exact, variant (mobile/AMP/canonical/tracking parameters) and fuzzy with interactive review | `dedupe`, `clean-urls`, `canonical` |
| **Link health**: retries, per-host throttling, multi-run confirmation, Wayback lookup, safe 301 rewrite | `check`, `prune` |
| **Archive**: sanitised single-file page snapshots (scripts removed, strict CSP), PDF text, robots.txt respected | `archive` |
| **Search** with operators: words, `"phrases"`, `-exclude`, `tag:` `folder:` `site:` `status:` `is:` `before:` `after:` `title:`; Persian/Arabic letter variants match | `search` |
| **File and tag** with a model trained on your own folders, your own rules first, taxonomy rules, then the assistant for what is left | `learn`, `organize`, `rules`, `ai-batch`, `apply-plan` |
| **Decide in groups**: unfiled bookmarks grouped by site / saved-together, one decision per group | `triage` |
| **Learn from your decisions**: accepted / corrected / rejected suggestions are recorded and checked against the stated confidence | `feedback` |
| **Tags and titles**: tag clean-up and suggestions, title clean-up (counters, repeated site names), synonyms | `tags`, `autotag`, `titles`, `synonyms` |
| **Folder tree**: split bloated folders, fold tiny ones, merge look-alike names; flattening deep trees only when you ask for a maximum depth | `restructure`, `folders` |
| **Reading list**: unread / reading / read, favourites | `mark`, `reading-list` |
| **Guided clean-up, self-diagnosis, upkeep**: wizard, doctor, weekly non-destructive maintenance with desktop / e-mail / webhook summaries | `wizard`, `doctor`, `maintain`, `schedule`, `notify` |
| **Dashboard and UI**: offline HTML dashboard; local web UI on 127.0.0.1 | `dashboard`, `ui` |
| **Chromium extension**: save a bookmark into the folder you would choose, search the library, apply suggested moves in the browser | [extension/](.claude/skills/mh-bookmark-organizer/extension/README.md) |

`mhbm --help` lists all commands; every one accepts `--json` for machine-readable output, and destructive ones are dry runs unless `--apply`.

## MCP reference

The server speaks MCP over stdio (protocol revisions 2024-11-05 to 2025-11-25). Every argument is validated against a strict schema (no type coercion);
results are size-capped and say so when cut; each call runs in its own process with a time limit and cancellation.

### Tools (29)

| Kind | Tools |
|------|-------|
| **Read-only (13)** | `bm_stats` health report · `bm_search` full-text search · `bm_folders` · `bm_tags` · `bm_history` undoable operations · `bm_reading_list` · `bm_triage` group unfiled bookmarks and suggest a folder per group · `bm_feedback` how suggestions fared · `bm_rules` list / test / suggest your filing rules · `bm_browsers` readable browser stores on this machine · `bm_backups` · `bm_doctor` · `bm_learn` filing accuracy report |
| **Plan (6): change nothing, return a `plan_id`** | `bm_organize` folders and tags for unfiled bookmarks · `bm_dedupe` (exact or variant) · `bm_restructure` · `bm_autotag` · `bm_titles` · `bm_prune` links confirmed dead (or suspect / restricted) |
| **Apply (1)** | `bm_apply_plan` applies exactly the stored plan, once, within 24 hours, only if the library is unchanged since; snapshotted and undoable |
| **Direct edits (7)** | `bm_import_file` (HTML, Chrome / Firefox JSON, CSV, JSON, and the other supported formats; detected automatically) · `bm_import_browser` (chrome, chromium, brave, edge, vivaldi, opera, opera-gx, yandex, firefox, librewolf, waterfox, floorp, zen, safari) · `bm_tag` one bookmark · `bm_mark` reading state · `bm_rules_edit` · `bm_export` to a **new** file (html, chrome, csv, json, md, raindrop, pocket, linkwarden, karakeep, instapaper) · `bm_undo` |
| **Network (2)** | `bm_check_links` (with `wayback=true` the failing URLs are also sent to archive.org) · `bm_archive` |

Tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) are accurate, so clients can ask before the destructive ones.

### Prompts (6) and resources

Prompts: `tidy-unfiled`, `clean-dead-links`, `merge-duplicates`, `weekly-review`, `find-bookmark`, `restructure-folders` (slash commands in Claude Code; a prompt appears
only if every tool it needs is exposed). Resources: `mhbm://library/summary`, `mhbm://library/folders`, `mhbm://library/history`, `mhbm://library/rules`;
templates `mhbm://bookmark/{id}` and `mhbm://plan/{plan_id}`.

### Server options

| Option | Flag | Environment variable |
|--------|------|----------------------|
| Library folder | `--home DIR` | `MHBM_HOME` (default `./.mhbm`) |
| Only the tools that cannot write; library opened `mode=ro` | `--read-only` | `MHBM_MCP_READ_ONLY=1` |
| Hide the tools that contact other hosts | `--no-network` | `MHBM_MCP_NO_NETWORK=1` |
| Folders the file tools may use (repeatable; the library folder is always denied) | `--allow-dir DIR` | `MHBM_MCP_ALLOW_DIRS` (platform path separator) |
| Hide single tools | `--disable-tool NAME` | `MHBM_MCP_DISABLE_TOOLS` (comma-separated) |
| Result size cap | `--max-result-chars N` (default 30000) | |
| Ignore the folders the client announces (`roots/list`) | `--no-client-roots` | |

Without `--allow-dir`, the file tools use the folders the client announces as roots, else `$CLAUDE_PROJECT_DIR`, else the working directory.

## How accurate is the filing?

`organize` files only bookmarks that are still unfiled, using what you already filed. `mhbm learn` measures how well that works on *your* library by holding
out whole saving sessions (a bookmark is never judged by neighbours saved in the same sitting), and prints how many bookmarks get an answer at each confidence
target and how often those answers are right. `learn --seeds 3` repeats it on different splits.

The measured results below are from **one library**, the author's: 1,422 filed bookmarks in 171 folders, up to 10 levels deep, a quarter of the titles in Persian.
Yours will differ; run `mhbm learn` to see your own numbers.

- The exact folder is the top suggestion for **39%** of held-out bookmarks and among the top three for **53%**.
- At the default threshold (answer only when 85% sure, measured out-of-sample) roughly **one bookmark in five** is filed automatically and about **nine of ten of those are right**.
  Asking for 80% sure: 26% covered at 87% precision; 90%: 17% at 92%; 70%: 33% at 83%.
- The rest go to the assistant (`ai-batch`), to `triage`, or stay for you. In a very granular tree the model answers one or two levels up ("sure of the area, not the sub-folder") rather than guess.
- Part of the gap cannot be closed by any model that learns from examples: 18% of that library's bookmarks sit in a folder with no example from another sitting.
- More filed bookmarks help less and less (learning from a quarter, half, three quarters and all gives 26%, 35%, 38%, 40%); archived page text and your decisions (`feedback`) are what can still move it.
- `triage` groups unfiled bookmarks; a simulated user needed about a third to 40% fewer decisions than going one by one. Groups with real support (30%+) were right for about 71% of their members.
- Training is deterministic: the same library gives the same suggestions every time. It takes seconds to about a minute for a normal library (1,422 filed bookmarks: 41 s pure Python, 14 s with numpy, 9 s with numpy on 4 processes on the author's 4-core machine; 45,000 synthetic bookmarks: 129 s) and is cached until your filed bookmarks change.

The repository's own tests use a synthetic library whose vocabulary is far cleaner than real life, so their scores are much higher; they guard against regressions and are not an accuracy claim.

## Safety and privacy

- **Your data stays local.** There is no server, account or telemetry. Network use is limited to what you ask: link checks and page archives (and a Wayback lookup only with `--wayback` / `wayback=true`, which shares the URL with archive.org).
- **Browser profiles are never written by default.** You work from an export or a copy; writing back (`install-chrome`, `sync`) requires the browser to be fully closed and `--confirm-chrome-closed`, refuses if it detects a running browser, writes a temp file and renames, and keeps a backup.
- **Nothing changes without a preview**, every apply is all-or-nothing, snapshotted and undoable (link checks and archives only record observations and are not in the undo journal).
- **Network guard.** Only public `http(s)` addresses; every redirect is re-checked; the address that was validated is the address connected to (DNS-rebinding defence); IPv4 addresses hidden in IPv6 or legacy spellings are recognised. Through an HTTP proxy the proxy resolves names. It is a strong guard, not a sandbox.
- **Untrusted text.** Bookmark titles, notes and archived page text are treated as data, never instructions; archived pages are sanitised single files with a strict CSP (still open them as untrusted).
- **MCP server.** Pinned to one library folder; file tools reach only allowed directories, open files without following links, never overwrite and never touch the library folder; one child process per call with an allow-listed environment; `--read-only` opens the library read-only so nothing on disk changes.
- **Imports and exports are bounded.** Bookmark imports are capped at 64 MiB and 250,000 records. Obsidian export builds a complete vault in a staging directory and requires a new destination directory; it refuses to overwrite an existing vault.
- **Several processes at once** (a scheduled `maintain`, the web UI, the extension, your shell) can share a library (write-ahead logging with a lock wait). A library written by an older release is upgraded in one transaction after a copy is saved; one written by a *newer* release is refused instead of damaged.
- **Supply chain.** No runtime dependencies; GitHub Actions are pinned to commit SHAs; the release pipeline is built for PyPI trusted publishing with hash-pinned tools and reproducible builds.

The full threat model, what is out of scope, and hardening tips: [SECURITY.md](SECURITY.md). Report vulnerabilities privately:
<https://github.com/mhnadali/mh-bookmark-organizer/security/advisories/new>.

Keep the library folder readable only by you (`chmod 700`): bookmarks are browsing history.

## OpenAI Codex notes

Codex reads the same marketplace file as Claude Code (`.claude-plugin/marketplace.json`) and the plugin's own manifest `.codex-plugin/plugin.json`. What differs from Claude Code, and why:

| Claude Code | Codex |
|-------------|-------|
| Root `SKILL.md` is the skill; the plugin is the skill folder | Codex ignores a root `SKILL.md`; the Codex skill is `codex/skills/mh-bookmark-organizer/SKILL.md` (MCP-first, same safety rules) with `agents/openai.yaml` |
| Plugin settings (`${user_config.*}`) choose the library folder, read-only and no-network | Codex has no per-plugin settings and leaves such placeholders as literal text; it also passes a server only a few environment variables (`HOME`, `PATH`, ...) |
| The client announces the project folder (`roots`) | Codex does not tell the server which folders you work in |

So the Codex plugin starts the server through `scripts/codex_mcp.py`, which applies safe defaults: the library is **`~/.mh-bookmark-organizer`**, and the file tools may read and write only in
your **`Downloads`, `Documents` and `Desktop`** folders that exist (never in the library); relative file names resolve in the first of them. Put your export in one of those
folders and ask for it by name (`b.html`). If none of those folders exists, imports and exports are refused until you name a folder.

To choose the folders yourself, register the server by hand instead of installing the plugin:

On macOS and Linux:

```bash
codex mcp add bookmarks --env MHBM_HOME=~/bm-library --env MHBM_MCP_ALLOW_DIRS=~/Downloads:~/exports \
  -- python3 /path/to/mh-bookmark-organizer/.claude/skills/mh-bookmark-organizer/scripts/codex_mcp.py
```

On Windows, use `python` and Windows paths (put quotes around paths containing spaces):

```powershell
codex mcp add bookmarks --env MHBM_HOME="$HOME\bm-library" --env MHBM_MCP_ALLOW_DIRS="$HOME\Downloads;$HOME\exports" `
  -- python "D:\path\to\mh-bookmark-organizer\.claude\skills\mh-bookmark-organizer\scripts\codex_mcp.py"
```

(`;` separates folders on Windows; `--env MHBM_MCP_READ_ONLY=1` enables look-only mode, and `--env MHBM_MCP_NO_NETWORK=1` hides network tools.) Ensure the selected Python command is on the `PATH` inherited by Codex.

What was checked, with Codex CLI 0.163.0 in an isolated home: Codex installs the plugin from the marketplace, lists the skill and the server, starts the server, lists its 29 tools, 4 resources
and 2 resource templates, relays a tool call that imports a file from `Downloads`, and the server refuses a path outside the allowed folders. `tests/test_codex.py` repeats this whenever the `codex` command is installed.
**Not checked:** a model-driven Codex session (it needs a Codex login), and the plugin format is young, so newer Codex versions may change details.

## What has and has not been verified

**Verified**
- The test suite (about 680 tests) passes on Linux under Python 3.9 (pure) and 3.13 (with numpy); earlier releases were also run under 3.10 to 3.12, with and without numpy.
- A real 1,500-bookmark Chrome export: import, dedupe, URL clean-up, filing, restructuring, undo and HTML export.
- The Chromium `Bookmarks` file we write and patch, checked against headless Chromium 141 (checksum, ids, GUIDs, order, dates, and the browser's own rewrite).
- Firefox 157.0.1, Zen 1.23.2b and Floorp 12.20.1 (real Gecko, headless): profiles they write are read back exactly, including a killed browser's WAL and a running browser; our HTML export imports.
- The extension and the native host end to end in headless Chromium; 50,000 synthetic bookmarks; link checking and archiving against a local test server and a proxied sandbox.
- The Claude Code plugin and marketplace pass `claude plugin validate --strict`; the Codex plugin loads in the real Codex (above).

**Not verified: be careful and keep the backups the tool makes**
- Link checks and archiving against many real sites.
- macOS and Windows (the CI workflow covers them, but I have not seen it run).
- Real Brave / Edge / Vivaldi / Safari profiles; Firefox on macOS, Windows, Snap and Flatpak; LibreWolf and Waterfox (their folder layouts come from documentation, not from a run).
- Writing a `Bookmarks` file into a real Chrome profile that uses Chrome Sync or `AccountBookmarks`, or on Windows / macOS.
- Real zsh / fish completion; the Arabic, Persian, Spanish, French and German translations have not been reviewed by professionals.

Known limits: archiving fetches static HTML only (pages rendered by JavaScript yield little text); PDF text needs poppler's `pdftotext`; the learned model needs roughly 10+ filed bookmarks in 2+ folders and below about 150 uses a fixed, uncalibrated blend.

## FAQ

**Will it change my browser's bookmarks?** Not unless you tell it to write back, with the browser closed. The normal path is: export from the browser, tidy the library, export a new file, import that file in the browser.

**Can I undo?** Yes: every applied change is journaled (`history`, `undo`) and snapshotted (`backups`). Link-check and archive results are observations, not changes to your bookmarks.

**Does an AI see my bookmarks?** The assistant you use sees what the tools return to it in your session (titles, URLs, folders, counts), as with any tool it calls. The engine itself sends nothing anywhere; the filing model is a small statistical model trained locally.

**Is it safe to let the assistant run unattended?** Start the server with `--read-only` (and `--no-network`) to let it look and report. Plan tools never write, and `bm_apply_plan` is the single writer for bulk changes, so a client can ask you before that one tool.

**Where is my data, and how do I back it up?** In the library folder you chose. Close mhbm (or use `mhbm export --format json`) and copy the folder; do not keep it in a Dropbox / OneDrive folder.

**Something is wrong.** Run `mhbm doctor` (add `--network` to probe link checking, `--fix` to repair the safe things), then see [TROUBLESHOOTING.md](.claude/skills/mh-bookmark-organizer/TROUBLESHOOTING.md).

## Project status

- **Version:** 1.5.2 is the current version in the tree. 1.6.0 is unreleased work: the MCP server, Claude Code and Codex plugins, and PyPI packaging changes are in the repository but have not been published as a release.
- **Licence:** none chosen yet. Until a `LICENSE` file is added the default copyright rules apply (all rights reserved); the owner decides. The plugin manifests deliberately declare no licence.
- **PyPI:** not published. The release pipeline (`.github/workflows/release.yml`, [RELEASING.md](.claude/skills/mh-bookmark-organizer/RELEASING.md)) needs the licence, a PyPI trusted-publisher entry and two GitHub environments.
- **Support:** one-person project; see [SECURITY.md](SECURITY.md) for response expectations. Only the latest release receives fixes.

## Repository layout

```
.claude-plugin/marketplace.json                  marketplace shared by Claude Code and Codex (one plugin: mh-bookmark-organizer)
.claude/agents/mh-bookmark-organizer.md          the subagent (copied into the plugin by tools/sync_agent.py)
.claude/skills/mh-bookmark-organizer/            the skill, and the plugin root for both assistants
    SKILL.md  agents/  .mcp.json  .claude-plugin/plugin.json     Claude Code
    codex/skills/  .codex-plugin/plugin.json  .codex-mcp.json     OpenAI Codex
    scripts/mhbm/                                the engine (standard library only); scripts/mhbm/mcp/ is the MCP server; scripts/codex_mcp.py starts it for Codex
    extension/                                   the Chromium extension
    tests/  tools/  pyproject.toml  RELEASING.md ...
docs/phase3/                                     design notes and audit reports behind release 1.6.0
SECURITY.md                                      threat model and how to report a problem
```

Developing: `ruff check scripts tests`, `mypy scripts`, `python -m unittest discover -s tests` from `.claude/skills/mh-bookmark-organizer`; see [CONTRIBUTING.md](.claude/skills/mh-bookmark-organizer/CONTRIBUTING.md).
