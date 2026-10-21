"""Tell you what the scheduled maintenance found: desktop notification, e-mail and/or webhook.

Configured under ``"notify"`` in ``<home>/config.json``::

    {"notify": {"on": ["desktop", "email", "webhook"], "when": "problems",
                "email": {"host": "smtp.example.com", "port": 587, "starttls": true, "from": "me@example.com",
                          "to": ["me@example.com"], "username": "me@example.com"},
                "webhook": {"format": "slack"}}}

Secrets never live in the config file: the SMTP password comes from ``MHBM_SMTP_PASSWORD`` and the webhook URL from
``MHBM_WEBHOOK_URL``. ``when`` is ``always``, ``changes`` (something needs attention or changed) or ``problems``.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import smtplib
import ssl
import subprocess
import urllib.request
from email.message import EmailMessage
from urllib.parse import urlsplit

PROVIDERS = ("desktop", "email", "webhook")
WHEN = ("always", "changes", "problems")


class NotifyError(ValueError):
    pass


def should_send(when: str, summary: dict) -> bool:
    if when == "always":
        return True
    problems = summary.get("dead", 0) + summary.get("suspect", 0) + (1 if summary.get("offline") else 0)
    if when == "problems":
        return problems > 0
    return problems > 0 or summary.get("proposals", 0) > 0 or summary.get("duplicates", 0) > 0


def message(summary: dict) -> tuple:
    """``(subject, body)`` from a maintenance summary."""
    bits = []
    if summary.get("offline"):
        bits.append("network looked down - nothing was checked")
    if summary.get("dead"):
        bits.append(f"{summary['dead']} dead links")
    if summary.get("suspect"):
        bits.append(f"{summary['suspect']} suspect links")
    if summary.get("proposals"):
        bits.append(f"{summary['proposals']} filing proposals")
    if summary.get("duplicates"):
        bits.append(f"{summary['duplicates']} duplicates")
    subject = "mhbm: " + (", ".join(bits) if bits else "all quiet")
    lines = [subject, ""]
    for k, label in (("checked", "links checked"), ("archived", "pages archived"), ("proposals", "filing proposals"),
                     ("duplicates", "duplicates waiting"), ("dead", "dead"), ("suspect", "suspect")):
        if k in summary:
            lines.append(f"{label}: {summary[k]}")
    if summary.get("report"):
        lines += ["", f"report: {summary['report']}"]
    return subject, "\n".join(lines)


def _desktop(subject: str, body: str) -> str:
    system = platform.system()
    if system == "Linux" and shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name=mhbm", subject, body.splitlines()[2] if len(body.splitlines()) > 2 else ""], check=True, timeout=10)
        return "notify-send"
    if system == "Darwin" and shutil.which("osascript"):
        def esc(s):  # AppleScript string literal
            return s.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e", f'display notification "{esc(body.splitlines()[2] if len(body.splitlines()) > 2 else "")}" with title "{esc(subject)}"'],
                       check=True, timeout=10)
        return "osascript"
    if system == "Windows" and shutil.which("powershell"):
        ps = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
              "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
              "$n.ShowBalloonTip(10000, $env:MHBM_TITLE, $env:MHBM_BODY, 'Info'); Start-Sleep -Seconds 11; $n.Dispose()")
        env = {**os.environ, "MHBM_TITLE": subject, "MHBM_BODY": body}  # passed via the environment: no quoting problems
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], env=env)
        return "powershell"
    raise NotifyError("no desktop notification tool found (notify-send, osascript or PowerShell)")


def _email(subject: str, body: str, cfg: dict) -> str:
    try:
        host, sender, to = cfg["host"], cfg["from"], cfg["to"]
    except KeyError as e:
        raise NotifyError(f"email needs {e.args[0]!r} in the notify.email settings") from e
    to = [to] if isinstance(to, str) else list(to)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(to)
    msg.set_content(body)
    port = int(cfg.get("port", 587))
    password = os.environ.get("MHBM_SMTP_PASSWORD", "")
    ctx = ssl.create_default_context()
    if cfg.get("ssl"):
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
    else:
        smtp = smtplib.SMTP(host, port, timeout=20)
    with smtp:
        smtp.ehlo()
        if not cfg.get("ssl") and cfg.get("starttls", True):
            smtp.starttls(context=ctx)
            smtp.ehlo()
        if cfg.get("username"):
            if not password:
                raise NotifyError("set the SMTP password in the MHBM_SMTP_PASSWORD environment variable")
            smtp.login(cfg["username"], password)
        smtp.send_message(msg)
    return f"{host}:{port}"


def _webhook(subject: str, body: str, summary: dict, cfg: dict) -> str:
    url = os.environ.get("MHBM_WEBHOOK_URL") or cfg.get("url", "")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotifyError("webhook: set MHBM_WEBHOOK_URL to an http(s) address")
    fmt = cfg.get("format", "json")
    payload = {"slack": {"text": body}, "discord": {"content": body[:1900]}, "ntfy": None}.get(fmt)
    headers = {"Content-Type": "application/json", "User-Agent": "mh-bookmark-organizer"}
    if fmt == "ntfy":  # ntfy.sh style: plain-text body, title in a header
        data, headers = body.encode("utf-8"), {"Title": subject.encode("ascii", "replace").decode(), "User-Agent": "mh-bookmark-organizer"}
    else:
        data = json.dumps(payload or {"subject": subject, "text": body, "summary": summary}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return f"{parts.hostname} -> HTTP {r.status}"


def send(summary: dict, cfg: dict, *, force: bool = False) -> list:
    """Send through every configured provider; returns ``[(provider, ok, detail)]``. Never raises for a single failure."""
    cfg = cfg or {}
    on = cfg.get("on") or []
    bad = [p for p in on if p not in PROVIDERS]
    if bad:
        raise NotifyError(f"unknown notify provider(s): {', '.join(bad)} (use {', '.join(PROVIDERS)})")
    when = cfg.get("when", "problems")
    if when not in WHEN:
        raise NotifyError(f"notify.when must be one of {', '.join(WHEN)}")
    if not on:
        return []
    if not force and not should_send(when, summary):
        return [("(all)", True, f"nothing to report (when={when})")]
    subject, body = message(summary)
    out = []
    for name in on:
        try:
            detail = (_desktop(subject, body) if name == "desktop" else _email(subject, body, cfg.get("email", {})) if name == "email"
                      else _webhook(subject, body, summary, cfg.get("webhook", {})))
            out.append((name, True, detail))
        except (NotifyError, OSError, subprocess.SubprocessError, smtplib.SMTPException, ValueError) as e:
            out.append((name, False, str(e)[:200]))
    return out
