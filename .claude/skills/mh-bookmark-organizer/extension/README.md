# mh-bookmark-organizer browser extension

A small Manifest V3 extension for Chrome, Edge, Brave, Vivaldi and Chromium that talks to **your local library** through
Chrome's native-messaging mechanism. Nothing is sent over the network: the browser starts `mhbm native-host` on your own
machine and exchanges messages with it over stdin/stdout.

## What it does

- **Save bookmark here**: suggests the folder you would pick (with alternatives as one-click chips) and tags, learned
  from your own library, then creates the bookmark in the browser *and* in the library. The popup tells the library what
  it proposed, so your choice (kept, or changed) is recorded: it checks the model's confidence and a folder you moved a
  page away from is not suggested for that page again.
- **Search your library**: the same full-text search as `mhbm search`, including archived page text.
- **Organize**: *Send my bookmarks to the library* copies the browser's tree to the library; *Suggest moves* shows what
  `organize` would file where; *Apply suggested moves in the browser* moves the bookmarks with the browser's own API
  (so Chrome Sync carries the change to your other devices). Nothing moves until you press the apply button.

## Install

1. `chrome://extensions` > turn on **Developer mode** > **Load unpacked** > choose this `extension/` folder.
   Copy the extension **ID** (32 letters).
2. Register the native host for that ID (once per browser and library):
   ```bash
   cd .claude/skills/mh-bookmark-organizer/scripts
   python3 -m mhbm --home ~/bm-library install-native-host --extension-id <ID> --browser chrome
   ```
   This writes a small launcher plus the host manifest into the browser's `NativeMessagingHosts` folder
   (the registry on Windows). `--browser` is one of chrome, chromium, brave, edge, vivaldi.
3. Restart the browser, open the extension popup. The status line should say it is connected.

If you later load the unpacked extension from a different folder its ID changes: run step 2 again.

## Permissions (and why)

| Permission | Used for |
|------------|----------|
| `bookmarks` | read the tree, create bookmarks, move bookmarks you approved |
| `nativeMessaging` | talk to the local `mhbm native-host` |
| `activeTab`, `tabs` | read the title and URL of the page you are saving |

There are no host permissions and no content scripts: the extension never reads page contents or contacts a website.
The native-host manifest allows only this extension's origin.

## Limits

- The browser caps host-to-extension messages at 1 MB. A bigger reply is refused with an error ("ask for fewer results"); the popup asks for limited batches.
- The first request after a library change trains the filing model and can take about a minute on a large library.
- Firefox and Safari are not supported (different extension and native-messaging manifests). Use `mhbm import-browser`.
- **Verified**: logic tests in Node, and an end-to-end run in headless Chromium (load the extension, send a tree, get a plan,
  apply moves). **Not verified**: Edge, Brave, Vivaldi and Windows/macOS native-host registration on real installs.

## Development

`npm test` runs the unit tests for `lib.js` (Node 20+). The Python suite (`tests/test_extension.py`) also drives the real
extension in headless Chromium when Playwright is available.
