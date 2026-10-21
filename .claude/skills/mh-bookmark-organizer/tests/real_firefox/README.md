# Real-Gecko harness

Drives a **real** headless Firefox (or Zen, Floorp, ... any Gecko build) to write profiles that `mhbm` then reads.
Everything is standard library; nothing runs unless `MHBM_FIREFOX_DIR` points at a directory containing the `firefox`
executable (an extracted Mozilla Linux tarball, or the `bin/FirefoxApp` directory of the conda-forge `firefox` package).
`MHBM_GECKO_EXE` names the executable when it is not `firefox` (e.g. `zen`, `floorp`).

| script | what it does |
| --- | --- |
| `build_profile.py <profile> <source.html> [--extras] [--bulk N] [--end clean\|kill\|term]` | Firefox imports a Netscape file through `browser.places.importBookmarksHTML`, optional extras go in through `PlacesUtils`, then it quits cleanly or is killed (WAL left behind). Writes `<profile>.truth.json` (Gecko's own tree), a JSON backup and an HTML export. |
| `live_test.py <profile> <work> --probe` | Firefox keeps running on a copy of the profile while mhbm reads it; checks the result equals Gecko's truth. Without `--probe` a stress test makes Firefox commit and checkpoint continuously. |
| `discover_layout.py <fresh-home>` | Starts Firefox with no `-profile` and prints where it put `profiles.ini`, `installs.ini` and the profile. |
| `compare.py <profile>` | Diffs what mhbm reads (places.sqlite, jsonlz4, JSON backup, HTML export) with the truth. |
| `make_fixture.py <profile> <out>` | Turns a profile into the small committed fixtures under `tests/fixtures/firefox/`. |
| `make_netscape.py <outdir>` | The synthetic source files (`rich.html`, `messy.html`). |
| `netns_exec.py` | Used with `unshare -n`: brings up loopback only, so the browser cannot reach the network. |

The browser is started with `HOME` pointing at a scratch directory, telemetry/update/Remote Settings disabled and no route to the network.
Regenerate the fixtures after a Firefox major release: `build_profile.py p rich.html --extras --end clean` then `make_fixture.py p ../fixtures/firefox`.
