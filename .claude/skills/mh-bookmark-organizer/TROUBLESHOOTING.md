# Troubleshooting

Start with `mhbm doctor` (add `--network` to probe the link-check path, `--fix` to repair what is safe to repair). It
checks Python and SQLite (FTS5), proxy and DNS, optional tools, the library folder, the search index, snapshot files and the
filing model, and prints a hint for everything that is not fine.

## Installing and running

| Symptom | Cause and fix |
|---------|---------------|
| `No module named mhbm` | Run from `.claude/skills/mh-bookmark-organizer/scripts`, set `PYTHONPATH` to that folder, or `pip install .` in the skill folder to get an `mhbm` command. |
| `python: command not found` / old Python | Python 3.9 or newer is required. Try `python3`, or `py -3` on Windows. |
| Search says "FTS5 unavailable" | Your SQLite lacks FTS5. Search still works with plain matching (slower, no ranking). Newer Python builds include it. |
| Output shows boxes or reversed text | Terminals without bidirectional support mis-order Persian/Arabic. `MHBM_BIDI=0` turns the direction marks off; `--lang en` forces English messages. |
| Where is my data? | `--home DIR`, else `$MHBM_HOME`, else `./.mhbm`. `mhbm config` shows the effective settings; the library is `library.db` plus `snapshots/`, `archive/`, `reports/`, `rules.json`, `model.json`. |

## The library file and several processes

| Symptom | Cause and fix |
|---------|---------------|
| "the library is busy" (exit code 75) | Another mhbm process (a scheduled `maintain`, the web UI, the extension) held the library for longer than the wait. Nothing was lost. Try again, or raise the wait: `MHBM_BUSY_TIMEOUT_MS=120000` (default 30000). |
| "this library uses layout N, which is newer than this mhbm understands" | The library was last opened by a newer mhbm. Upgrade mhbm; nothing was changed. Never point an old version at a library a newer one has used. |
| `library-schema*.db` files in `backups/` | A copy of the library file saved right before an automatic upgrade to a newer layout. Delete it once you are happy. To go back to it, close every mhbm process, delete `library.db-wal` and `library.db-shm` if they exist, and copy the file over `library.db`. |
| `library.db-wal` and `library.db-shm` next to the library | Normal: write-ahead logging keeps readers and one writer from blocking each other. They disappear when the last process closes the library. Copy or back up the folder only while mhbm is closed (or use `export --format json`). |
| The library is on a network share or a synced folder and fails with "disk I/O error" or "locking protocol" | Some file systems cannot share the memory-mapped file WAL needs. `MHBM_JOURNAL_MODE=delete` turns WAL off (processes then simply wait for each other). A library inside Dropbox/OneDrive can be corrupted by the sync client; keep it in a local folder. |
| `doctor` says "write-ahead logging is off here" | Either you set `MHBM_JOURNAL_MODE=delete`, or the file system refused WAL. Harmless for one process at a time. |

## Importing and exporting

| Symptom | Cause and fix |
|---------|---------------|
| "cannot detect format" | Pass `--format html\|chrome\|firefox\|csv\|json`. |
| Imported 0 bookmarks from a file you already imported | By design: a bookmark already present in the same folder is skipped. `--allow-duplicates` imports it anyway; `--show-skipped` lists what was skipped. |
| Import from a browser fails | Close the browser (profile files are locked on Windows) or use `import-browser NAME --profile "Profile 1"`. `mhbm browsers` lists what was found. Safari needs Full Disk Access for the terminal on macOS. |
| Chrome ignores the exported HTML | In Chrome use **Bookmarks > Import bookmarks and settings > Bookmarks HTML file**. It adds an "Imported" folder; delete the old folders afterwards. |
| `install-chrome` / `sync` refuses to run | The browser must be completely closed and you must pass `--confirm-chrome-closed`. If Chrome Sync is on, it can overwrite local edits: prefer the HTML import route. Run `verify-chrome FILE` first. |
| `import-browser chrome` imported the wrong profile | It imports the profile the browser opens (last used, else `Default`). `mhbm browsers` lists every profile with its folder name and display name; choose with `--profile "Work"` or `--profile "Profile 1"`, or take them all with `--all-profiles`. |
| A profile is listed with a note instead of a path | Its bookmarks cannot be read: `EncryptedBookmarks2` (encrypted by the browser) or Safari without Full Disk Access. Export HTML from the browser and `import FILE`. |
| `install-chrome` says the profile uses Chrome Sync | The sync engine can restore the old bookmarks over what is written. Turn Sync off for bookmarks, or pass `--allow-synced-profile` if you accept that. |
| `install-chrome` wrote something I did not want | `install-chrome FILE --restore --confirm-chrome-closed` puts the newest backup back (`--restore BACKUPFILE` for another one); what it replaces is kept as another backup. |
| "Close the browser completely first" (from `install-chrome` / `sync`) | A Chromium-family browser still holds the profile (`SingletonLock` on Linux/macOS, `lockfile` on Windows). It rewrites `Bookmarks` when it saves, which would undo our write while the library believed it happened. Quit it completely (also background processes), then retry. A stale lock left by a crash does not count: the process id in it is checked. A lock made on another computer, or inside a Snap/Flatpak sandbox, cannot be checked and counts as running; if you are sure the browser is closed, add `--ignore-profile-lock`. |
| `install-chrome` / `sync` say some bookmarks were "left out" or "kept in the library only" | Chromium silently drops a bookmark whose URL it cannot parse (no scheme, empty host, bad port, e.g. `example.com` or `http://`) and, on Chromium 143 and older, renumbers every id when that makes the checksum wrong. They stay in your library; fix the URL (`tag ID` / `clean-urls`) to sync them. |
| After upgrading, the first `sync` re-adds a bookmark I had deleted in the browser | Sync keys changed to match how the browser re-encodes URLs (spaces, non-ASCII, IDN, `..`). A bookmark whose key changed is not recognised in the old record for that one run. Nothing is deleted because of it; delete it again. |
| `import-browser firefox` finds nothing, or reads the wrong profile | Firefox 147+ on Linux keeps profiles in `~/.config/mozilla/firefox` (older ones, and any home that already had `~/.mozilla`, in `~/.mozilla/firefox`). `mhbm browsers` lists every profile, the one the installation really uses first (`installs.ini`). Pick another with `--profile PATH` (a profile folder, `places.sqlite` or a `bookmarks-*.jsonlz4`). LibreWolf, Waterfox, Floorp and Zen are read the same way. |
| "places.sqlite kept changing while it was copied" | The browser was writing its bookmarks at that very moment. Run the command again, or close the browser. (Firefox keeps the database locked, so mhbm works on a consistent copy of it and its `-wal` file.) |
| "not a readable Firefox places.sqlite" | The file is not a Firefox database or is damaged; use the newest file in the profile's `bookmarkbackups/` instead. |
| Firefox backup (`.jsonlz4`) is "corrupt" | The file is truncated or is not a bookmarks backup. Use a copy from `bookmarkbackups/` in the profile, or export HTML from Firefox. |

## Link checking and archiving

| Symptom | Cause and fix |
|---------|---------------|
| "no link answered; nothing was recorded" | Offline, firewall, or an HTTP proxy blocks the checker. Nothing is marked dead in that case. Check `doctor --network`. |
| Everything shows `suspect` | Expected on the first run: a link becomes `dead` only after it fails repeatedly over several hours or days. Re-run `check --recheck-suspect` later. |
| Sites show `restricted` | They answer 401/403/429 to bots. They are alive; never prune them. |
| A local or intranet address is "blocked" | The guard only reaches public http(s) hosts on purpose (it blocks loopback, private ranges and local names). |
| PDFs are archived but not searchable | Install poppler (`pdftotext`). |
| Archived page is nearly empty | The page builds its content with JavaScript; only static HTML is fetched. |

## Filing and accuracy

| Symptom | Cause and fix |
|---------|---------------|
| `organize` proposes nothing | Fewer than 10 filed bookmarks in 2 or more folders, or nothing reached the confidence threshold. Run `mhbm learn` to see what is achievable; lower `--min-confidence` gradually, or let the agent do the rest with `ai-batch`. |
| "sure of the area, not the sub-folder" | The model recognised the topic but not the exact folder in a deep tree, so it offers the parent. Accept it, or file the bookmark by hand. |
| Accuracy numbers jump between runs | Small libraries are noisy. `mhbm learn --seeds 3` averages three different splits and shows the range. |
| Training is slow | It happens once and is cached in `model.json` until your filed bookmarks change. About a minute for 1,500 bookmarks, a few seconds for a few hundred. Archive pages first (`archive`) for better predictions. |
| Wrong suggestions after you reorganised folders | `mhbm learn --retrain` discards the cached model. |

## Speed

| Symptom | Cause and fix |
|---------|---------------|
| The first `organize` / `learn` takes a minute | It trains once and is cached in `model.json`. `pip install numpy` (or `pip install .[fast]`) makes it several times faster with identical results; `doctor` says whether numpy is in use. |
| Training is slow on a big library | Judging is sampled above about 6,000 filed bookmarks and runs in parallel worker processes (one per core, up to the number of folds). `MHBM_WORKERS=1` turns the processes off, `MHBM_WORKERS=N` sets the number. |
| "Cannot start worker processes" / sandbox limits | It quietly falls back to one process; nothing to do. |
| Calling `mhbm.learn` from your own script on Windows or macOS runs the script twice | Worker processes start by importing your script: put the code under `if __name__ == "__main__":`. The `mhbm` command itself is already safe. |
| Same library, different suggestions between runs | Should not happen any more (training is deterministic). If it does, run `mhbm doctor --json` and report it. |

## Triage and feedback

| Symptom | Cause and fix |
|---------|---------------|
| `triage` says there are as many decisions as bookmarks | Nothing links them: no site appears twice and nothing was saved together (or the bookmarks have no save date). Use `organize`, or file by hand. |
| A group's suggestion is clearly wrong | Support under about 30% means "no real idea" (groups are sorted so the confident ones come first). Pick an alternative, type a folder, or `r` to reject: it will not be suggested again. |
| `feedback` shows no explicit decisions | Plan files applied without looking are recorded but not counted. Use `review`, `triage --interactive`, the web UI or the extension. |
| Suggestions seem less confident than before | The model's confidence is corrected towards what you actually accepted once there are 30 explicit decisions. `feedback` shows quoted vs accepted; `feedback clear --apply` forgets the record. |
| I rejected a folder by mistake | `undo` the operation that recorded it (the decision is forgotten and the folder is suggested again), or `feedback clear --apply`. |
| `learn --curve` / `--text-effect` say "not enough" | They need at least 150 filed bookmarks (the calibrated model's minimum); `--text-effect` also needs archived pages. |

## Undo and recovery

- `mhbm history` lists operations; `mhbm undo` reverts the last one (`--op N` for a specific one). Undo also restores deleted bookmarks and empty folders.
- Every `--apply` also writes a snapshot; `mhbm backups` lists them and `backups --restore FILE` restores one.
- The journal keeps the last 50 operations. Older changes are only recoverable from snapshots.

## Browser extension and local UI

| Symptom | Cause and fix |
|---------|---------------|
| Extension says "native host not found" | Run `mhbm install-native-host --extension-id <id from chrome://extensions> [--browser chrome\|edge\|brave\|chromium]`, then restart the browser. The id changes if you reload an unpacked extension from another folder. |
| "the host took too long" | The first request trains the filing model; try again after it finishes. |
| `ui` prints a URL ending in `#token=...` | That is intended: the token lives in the URL fragment and is never sent to the server. Open the whole URL. |

## Still stuck

Run `mhbm doctor --json` and open an issue with its output (it contains no bookmark data) and the exact command you ran.
