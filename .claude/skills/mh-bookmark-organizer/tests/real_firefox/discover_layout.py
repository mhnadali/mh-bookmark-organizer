#!/usr/bin/env python3
"""Where does a REAL Firefox put its profile when started with NO -profile argument?  (Linux layout discovery.)

usage: MHBM_FIREFOX_DIR=... python3 -I discover_layout.py <fresh-home-dir> [--env KEY=VAL ...] [--args ...]
Starts headless Firefox with HOME=<fresh-home-dir> (so nothing real is touched), waits for the profile to appear, quits cleanly,
then prints the tree of files created under HOME that matter (profiles.ini, installs.ini, Profile Groups, places.sqlite).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ffenv
import marionette


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("home")
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--args", nargs="*", default=[])
    ap.add_argument("--wait", type=float, default=6)
    ap.add_argument("--mkdir", action="append", default=[], help="relative dir to create under HOME before starting")
    ap.add_argument("--js", default="", help="extra chrome JS to run (function body) before quitting; result printed")
    a = ap.parse_args()
    ffenv.ensure_netns()
    home = Path(a.home).resolve()
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    for rel in a.mkdir:
        (home / rel).mkdir(parents=True, exist_ok=True)
    d = ffenv.firefox_dir()
    import subprocess
    env = ffenv.env_for(home)
    for kv in a.env:
        k, v = kv.split("=", 1)
        env[k] = v
    # NB: ffenv.env_for sets XDG_*; drop them unless explicitly given so we see Firefox's own default choice
    for k in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        if not any(kv.startswith(k + "=") for kv in a.env):
            env.pop(k, None)
    cmd = [str(d / os.environ.get("MHBM_GECKO_EXE", "firefox")), "-headless", "-no-remote", "-marionette", "--remote-allow-system-access", *a.args]
    with open(home / "ff.log", "wb") as log:
        p = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        m = marionette.Marionette()
        m.connect(60)
        m.new_session()
        m.chrome()
        time.sleep(a.wait)
        info = m.run("""
          const dir = Services.dirsvc.get("ProfD", Ci.nsIFile).path;
          const svc = Cc["@mozilla.org/toolkit/profile-service;1"].getService(Ci.nsIToolkitProfileService);
          return {ProfD: dir, version: Services.appinfo.version, profileCount: svc.profileCount,
                  default: svc.defaultProfile && svc.defaultProfile.rootDir.path, current: svc.currentProfile && svc.currentProfile.rootDir.path,
                  profiles: Array.from(svc.profiles, p => ({name: p.name, root: p.rootDir.path, local: p.localDir.path}))};
        """)
        print("firefox says:", {k: (v if not isinstance(v, str) else v.replace(str(home), "$HOME")) for k, v in info.items() if k in ("ProfD", "version", "profileCount")})
        if a.js:
            print("js result:", str(m.run(a.js)).replace(str(home), "$HOME"))
        m.quit(("eAttemptQuit",))
        p.wait(30)
    finally:
        ffenv.kill_group(p)
    print("files under HOME (depth<=4, interesting only):")
    for root, dirs, files in os.walk(home):
        depth = Path(root).relative_to(home).parts
        if len(depth) > 4:
            dirs[:] = []
            continue
        for f in sorted(files):
            if f in ("profiles.ini", "installs.ini", "places.sqlite", "compatibility.ini") or f.endswith(".ini") or f.startswith("Profile Groups") or (f.endswith("sqlite") and "Profile" in root):
                print("  ", Path(root, f).relative_to(home))
        for dn in sorted(dirs):
            if dn in ("Profile Groups", "Profiles", "firefox", "mozilla", ".mozilla"):
                print("   [dir]", Path(root, dn).relative_to(home))
    for f in home.rglob("*.ini"):
        if f.name in ("profiles.ini", "installs.ini"):
            print(f"=== {f.relative_to(home)}")
            print(f.read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
