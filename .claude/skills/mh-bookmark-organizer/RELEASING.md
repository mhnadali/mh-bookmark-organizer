# Releasing

A release is a tag. Everything else is checked by machines, and a person approves the upload.

```bash
# 1. on main, with CI green: bump the version everywhere it is written (one test, tests/test_repo_hygiene.py, fails until they agree)
#    scripts/mhbm/__init__.py   extension/manifest.json   .claude-plugin/plugin.json   README.md ("Version X.Y.Z")   CHANGELOG.md ("## X.Y.Z")
python tools/release_check.py v1.6.0 --notes /tmp/notes.md     # the same gate the workflow runs first; prints what is wrong
#    tests/test_plugin.py runs `claude plugin validate --strict` when the claude command is installed: run the suite once where it is (CI has no claude command)
# 2. rehearse: Actions > release > Run workflow, tick "testpypi" (builds, tests the sdist, installs the wheel on 3 OSes x Python 3.9-3.13,
#    uploads to TestPyPI only)
# 3. tag the commit you rehearsed (the files are byte-identical, the build is reproducible) and push the tag
git tag -s v1.6.0 -m "mh-bookmark-organizer 1.6.0" && git push origin v1.6.0
# 4. approve the "pypi" environment when the workflow asks; it verifies what PyPI serves against what it built
```

What the workflow does, in order: `verify` (tag = code = extension manifest = plugin manifest = README = CHANGELOG, and the commit is on the default
branch) -> `build` (hash-pinned tools, `twine check --strict`, `check-wheel-contents`, `validate-pyproject`, the release gate below, and a second
build that must be byte-identical) -> `sdist-tests` (the whole test suite, run from the unpacked sdist) -> `smoke` (the wheel and the sdist installed
into fresh virtual environments and used: import, console scripts, doctor, import, search, learn, the MCP server over stdio, the web UI) ->
`attest` (build provenance) -> TestPyPI -> install from TestPyPI -> PyPI (a person approves) -> install from PyPI -> GitHub release with checksums.

## Things only the owner of the repository can do (once)

| What | Why | Where |
|------|-----|-------|
| Choose a licence: add the `LICENSE` file **in the skill folder** (`.claude/skills/mh-bookmark-organizer/LICENSE`: that is the build root, a copy at the repository root is in neither the sdist nor the wheel; add one there too if GitHub should show it), the `license` metadata in `pyproject.toml`, and `license` in `.claude-plugin/plugin.json` | The release gate (`MHBM_RELEASE=1 python -m unittest tests/test_packaging.py`) fails without it, so nothing can be uploaded by accident. Nobody else can pick it for you. | skill folder, `pyproject.toml`, `.claude-plugin/plugin.json` |
| Reserve the project name with a *pending publisher* on PyPI and on TestPyPI | Publishing uses short-lived tokens (OpenID Connect), no stored secrets. Owner: `mhnadali`, repository: `mh-bookmark-organizer`, workflow file: `release.yml`, environment: `pypi` (TestPyPI: `testpypi`). | pypi.org/manage/account/publishing, test.pypi.org/manage/account/publishing |
| Create the GitHub environments `testpypi` and `pypi`; add yourself as a *required reviewer* of `pypi` | The upload job waits for a person. | repository Settings > Environments |
| Turn on *private vulnerability reporting* | `SECURITY.md` asks people to report privately there. | repository Settings > Code security |
| Protect the `v*` tags | Only maintainers can create a release tag. | repository Settings > Rules |

The name `mh-bookmark-organizer` was not taken on PyPI when this was written (2026-10-16); check again before the first upload.

## Updating the pinned tools

`requirements-release.txt` is generated; edit `requirements-release.in` and run the command in its header. Dependabot proposes updates weekly.
GitHub Actions in both workflows are pinned to full commit SHAs with the version in a comment; resolve a tag with
`git ls-remote https://github.com/<owner>/<action> "refs/tags/<tag>" "refs/tags/<tag>^{}"` and use the *peeled* (`^{}`) line when there is one.

## Not part of the wheel

The wheel is the engine and the two console scripts (`mhbm`, `mhbm-mcp`). The browser extension, the skill, the subagent and the Claude Code plugin are
in the sdist and in the repository; install the plugin with `/plugin marketplace add mhnadali/mh-bookmark-organizer`.
