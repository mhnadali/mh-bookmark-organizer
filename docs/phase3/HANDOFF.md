# Development handoff (current)

**Updated:** 2026-10-22. This document describes the current checkout; the older planning snapshot is preserved in [`HANDOFF-2026-10-16-ARCHIVE.md`](HANDOFF-2026-10-16-ARCHIVE.md) for historical context only. Do not apply patches or follow implementation instructions from that archive without checking the current source first.

## Source of truth

- Repository branch: `main`; reviewed base commit: `dd7a9ba`.
- Current version in source and plugin manifests: `1.5.2`.
- `1.6.0` is unreleased work. Keep the changelog and README explicit; do not tag or publish until the release gate passes.
- The owner has not selected a license. Do not add a license identifier or publish to PyPI before that decision.
- The current working tree contains a local Windows Codex launcher adjustment in `.codex-mcp.json` (`python` instead of `python3`). Preserve it unless deliberately replacing it with a tested cross-platform setup.

## Work completed in this development pass

- Obsidian exports now stage a complete vault and refuse existing destination directories.
- Imports have shared limits of 64 MiB and 250,000 bookmarks; MCP copies the verified descriptor to a private temporary file in bounded chunks.
- MCP opened-file verification fails closed when the platform cannot resolve the descriptor; Windows uses `GetFinalPathNameByHandleW`.
- CI no longer soft-fails real Chromium/Firefox jobs and includes Python 3.11 on macOS and Windows.
- Codex setup documentation now distinguishes Windows Python commands from macOS/Linux commands.
- Empty or malformed logistic-regression input now raises an explicit `ValueError`.

The items above are working-tree changes and remain uncommitted until reviewed and accepted.

## Verification status

Passed locally on Windows with Python 3.15:

- Full suite: 692 tests, all passed or skipped for documented platform/optional-dependency reasons (80 skipped).
- `ruff check scripts tests tools` passed.
- `git diff --check` passed.
- Windows handle-path, import limits, atomic Obsidian staging, profile backup/sync, and HTTP UI regressions are covered by the full suite.
- `npm ci` for the real-browser harness completed with zero reported vulnerabilities.

Not yet verified:

- Optional real Codex CLI integration (`MHBM_TEST_REAL_CODEX=1`); it is excluded from normal tests because the locally installed Codex CLI could not open its temporary fixture through the app-server tool call.
- Real-browser Chromium test module on this Windows host; GitHub's required Linux/Xvfb job is the intended integration gate.
- GitHub Actions on Linux/macOS/Windows, including the now-required real-browser jobs.
- Real Firefox integration on a hosted runner.
- PyPI publication, because license and trusted-publisher configuration require owner action.

## Next actions

1. Confirm the Chromium and Firefox jobs on GitHub before relying on them as branch-protection checks.
2. Review the final diff, especially the local Codex launcher change and Windows file-handle policy.
3. Obtain the owner's license choice and configure the PyPI trusted publisher before any release operation.
