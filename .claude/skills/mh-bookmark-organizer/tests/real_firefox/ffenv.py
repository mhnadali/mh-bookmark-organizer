"""Shared helpers: locate the Gecko binary, write user.js, launch Firefox headless inside a loopback-only netns.

The binary is located through $MHBM_FIREFOX_DIR (directory that contains the `firefox` executable, i.e. the
extracted Mozilla tarball / conda-forge `bin/FirefoxApp`).  Everything is stdlib-only.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Prefs that keep a throw-away profile quiet and offline.  (The netns is the real guarantee; these stop noisy retries.)
QUIET_PREFS = {
    "datareporting.policy.dataSubmissionEnabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "toolkit.telemetry.enabled": False,
    "toolkit.telemetry.unified": False,
    "toolkit.telemetry.server": "http://127.0.0.1:9/",
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "app.update.enabled": False,
    "app.update.auto": False,
    "app.update.checkInstallTime": False,
    "app.update.service.enabled": False,
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage": "about:blank",
    "browser.startup.page": 0,
    "browser.aboutwelcome.enabled": False,
    "browser.disableResetPrompt": True,
    "browser.region.update.enabled": False,
    "browser.search.update": False,
    "browser.safebrowsing.malware.enabled": False,
    "browser.safebrowsing.phishing.enabled": False,
    "browser.safebrowsing.downloads.enabled": False,
    "extensions.update.enabled": False,
    "extensions.blocklist.enabled": False,
    "app.normandy.enabled": False,
    "services.settings.server": "http://127.0.0.1:9/v1",
    "network.captive-portal-service.enabled": False,
    "network.connectivity-service.enabled": False,
    "network.http.speculative-parallel-limit": 0,
    "network.dns.disablePrefetch": True,
    "network.prefetch-next": False,
    "network.proxy.type": 1,                      # blackhole proxy: belt and braces next to the netns
    "network.proxy.http": "127.0.0.1", "network.proxy.http_port": 9,
    "network.proxy.ssl": "127.0.0.1", "network.proxy.ssl_port": 9,
    "network.proxy.no_proxies_on": "",
    "startup.homepage_welcome_url": "",
    "startup.homepage_welcome_url.additional": "",
    "toolkit.startup.max_resumed_crashes": -1,
    "browser.tabs.warnOnClose": False,
    "browser.sessionstore.resume_from_crash": False,
}


def firefox_dir() -> Path:
    d = os.environ.get("MHBM_FIREFOX_DIR")
    if not d:
        raise SystemExit("set MHBM_FIREFOX_DIR to the directory containing the `firefox` executable")
    return Path(d)


def js_pref(name: str, value) -> str:
    if isinstance(value, bool):
        v = "true" if value else "false"
    elif isinstance(value, int):
        v = str(value)
    else:
        v = '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f'user_pref("{name}", {v});\n'


def write_user_js(profile: Path, prefs: dict) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    allprefs = {**QUIET_PREFS, **prefs}
    (profile / "user.js").write_text("".join(js_pref(k, v) for k, v in allprefs.items()), encoding="utf-8")


def env_for(home: Path) -> dict:
    d = firefox_dir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MOZ_", "XDG_")) and k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    for k in list(env):
        if k.lower().endswith("_proxy"):
            del env[k]
    env.update({
        "HOME": str(home), "LD_LIBRARY_PATH": str(d),
        "MOZ_HEADLESS": "1", "MOZ_CRASHREPORTER_DISABLE": "1", "MOZ_DISABLE_CONTENT_SANDBOX": "1",
        "MOZ_DISABLE_GMP_SANDBOX": "1", "MOZ_DISABLE_RDD_SANDBOX": "1", "MOZ_DISABLE_SOCKET_PROCESS_SANDBOX": "1",
        "MOZ_DISABLE_AUTO_SAFE_MODE": "1", "MOZ_NO_REMOTE": "1", "MOZ_SERVICES_HEALTHREPORT": "0",
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_HOME": str(home / ".local/share"),
    })
    return env


def launch(profile: Path, home: Path, *, extra_args=(), log: Path | None = None, netns: bool = True) -> subprocess.Popen:
    """Start Firefox headless on ``profile``.  Returns the Popen (own process group; kill with ``kill_group``)."""
    d = firefox_dir()
    home.mkdir(parents=True, exist_ok=True)
    cmd = [str(d / os.environ.get("MHBM_GECKO_EXE", "firefox")), "-headless", "-profile", str(profile), "-no-remote", *extra_args]
    if netns:
        cmd = ["unshare", "-n", sys.executable, "-I", str(HERE / "netns_exec.py"), "--"] + cmd
    if log is None:
        return subprocess.Popen(cmd, env=env_for(home), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
    with open(log, "wb") as out:  # the child keeps its own duplicate of the descriptor
        return subprocess.Popen(cmd, env=env_for(home), stdout=out, stderr=subprocess.STDOUT, start_new_session=True)


def kill_group(p: subprocess.Popen, sig=signal.SIGKILL) -> None:
    try:
        os.killpg(p.pid, sig)
    except ProcessLookupError:
        pass
    try:
        p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        pass


def wait_for(path_pred, timeout: float, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if path_pred():
            return True
        time.sleep(step)
    return False


def ensure_netns() -> None:
    """Re-exec the current script inside a loopback-only network namespace (once)."""
    if os.environ.get("MHBM_IN_NETNS") == "1" or os.environ.get("MHBM_NO_NETNS") == "1":  # NO_NETNS: CI runners without unshare rights
        return
    os.environ["MHBM_IN_NETNS"] = "1"
    os.execvp("unshare", ["unshare", "-n", sys.executable, "-I", str(HERE / "netns_exec.py"), "--", sys.executable, "-I", *sys.argv])
