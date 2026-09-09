#!/usr/bin/env python3
"""
UXCam SDK release watcher.

Polls the authoritative package registries for each UXCam SDK, compares the
latest published version against a local state file, and emails a digest when
something new lands.

Stdlib only - no pip install required.

Usage:
    python check_uxcam.py                 # check, notify on change, update state
    python check_uxcam.py --dry-run       # check + print, never email, never write state
    python check_uxcam.py --force-notify  # email the current state even if nothing changed
    python check_uxcam.py --seed          # write state without emailing (first run)

Email is configured entirely by environment variables (see README).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

USER_AGENT = "uxcam-sdk-watch/1.0 (+QA release monitor)"
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3

DEFAULT_STATE_PATH = Path(__file__).with_name("state.json")

# Set "enabled": False to stop tracking a platform without deleting its config.
SOURCES: list[dict[str, Any]] = [
    {
        "key": "android",
        "label": "Android (native)",
        "coordinate": "com.uxcam:uxcam",
        "parser": "maven",
        "url": "https://repo1.maven.org/maven2/com/uxcam/uxcam/maven-metadata.xml",
        "changelog": "https://developer.uxcam.com/docs/android-changelog",
        "install_hint": "implementation 'com.uxcam:uxcam:{version}'",
        "enabled": True,
    },
    {
        "key": "ios",
        "label": "iOS (native)",
        "coordinate": "UXCam (CocoaPods)",
        "parser": "cocoapods",
        "url": "https://trunk.cocoapods.org/api/v1/pods/UXCam",
        "changelog": "https://developer.uxcam.com/docs/ios-sdk-change-log",
        "install_hint": "pod 'UXCam', '~> {version}'",
        "enabled": True,
    },
    {
        "key": "react-native",
        "label": "React Native",
        "coordinate": "react-native-ux-cam (npm)",
        "parser": "npm",
        "url": "https://registry.npmjs.org/react-native-ux-cam/latest",
        "changelog": "https://github.com/uxcam/react-native-ux-cam/releases",
        "install_hint": "npm install react-native-ux-cam@{version}",
        "enabled": True,
    },
    # --- Not tracked today. Flip "enabled" to True to start watching. --------
    {
        "key": "flutter",
        "label": "Flutter",
        "coordinate": "flutter_uxcam (pub.dev)",
        "parser": "pubdev",
        "url": "https://pub.dev/api/packages/flutter_uxcam",
        "changelog": "https://developer.uxcam.com/docs/flutter-changelog",
        "install_hint": "flutter_uxcam: ^{version}",
        "enabled": False,
    },
    {
        "key": "cordova",
        "label": "Cordova / Ionic",
        "coordinate": "cordova-uxcam (npm)",
        "parser": "npm",
        "url": "https://registry.npmjs.org/cordova-uxcam/latest",
        "changelog": "https://github.com/uxcam/cordova-uxcam/releases",
        "install_hint": "cordova plugin add cordova-uxcam@{version}",
        "enabled": False,
    },
]

PRERELEASE_RE = re.compile(r"(alpha|beta|rc|dev|snapshot|preview|pre)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def http_get(url: str) -> bytes:
    """GET with a small retry loop. Raises the last error if all attempts fail."""
    last_err: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
            last_err = err
            if attempt < HTTP_RETRIES:
                continue
    raise RuntimeError(f"GET {url} failed after {HTTP_RETRIES} attempts: {last_err}")


# --------------------------------------------------------------------------- #
# Version handling
# --------------------------------------------------------------------------- #


def is_stable(version: str) -> bool:
    return not PRERELEASE_RE.search(version)


def version_key(version: str) -> tuple[int, ...]:
    """Loose numeric sort key: '3.10.9' -> (3, 10, 9). Non-numeric parts ignored."""
    parts = re.split(r"[.\-+_]", version)
    nums: list[int] = []
    for part in parts:
        digits = re.match(r"^\d+", part)
        if not digits:
            break
        nums.append(int(digits.group()))
    return tuple(nums) or (0,)


def is_newer(candidate: str, known: str | None) -> bool:
    if not known:
        return False
    return version_key(candidate) > version_key(known)


def pick_latest_stable(versions: list[str]) -> str | None:
    stable = [v for v in versions if is_stable(v)]
    pool = stable or versions
    if not pool:
        return None
    return max(pool, key=version_key)


# --------------------------------------------------------------------------- #
# Registry parsers - each returns (version, published_iso_or_None)
# --------------------------------------------------------------------------- #


def parse_maven(raw: bytes) -> tuple[str | None, str | None]:
    root = ET.fromstring(raw)
    versioning = root.find("versioning")
    if versioning is None:
        return None, None

    versions = [
        el.text.strip()
        for el in versioning.findall("./versions/version")
        if el.text and el.text.strip()
    ]
    latest = pick_latest_stable(versions)

    if latest is None:
        release = versioning.findtext("release") or versioning.findtext("latest")
        latest = release.strip() if release else None

    published = None
    stamp = (versioning.findtext("lastUpdated") or "").strip()
    if len(stamp) == 14 and stamp.isdigit():
        published = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        ).isoformat()

    return latest, published


def parse_cocoapods(raw: bytes) -> tuple[str | None, str | None]:
    data = json.loads(raw)
    entries = data.get("versions") or []
    by_name = {
        e.get("name"): e.get("created_at")
        for e in entries
        if isinstance(e, dict) and e.get("name")
    }
    latest = pick_latest_stable(list(by_name))
    return latest, by_name.get(latest)


def parse_npm(raw: bytes) -> tuple[str | None, str | None]:
    data = json.loads(raw)
    version = data.get("version")
    # /latest manifests carry no timestamp; that's fine, the diff is what matters.
    return version, None


def parse_pubdev(raw: bytes) -> tuple[str | None, str | None]:
    data = json.loads(raw)
    latest = data.get("latest") or {}
    return latest.get("version"), latest.get("published")


PARSERS: dict[str, Callable[[bytes], tuple[str | None, str | None]]] = {
    "maven": parse_maven,
    "cocoapods": parse_cocoapods,
    "npm": parse_npm,
    "pubdev": parse_pubdev,
}


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as err:
        print(f"WARN: could not read state file ({err}); treating as first run.")
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #


def check_all(state: dict[str, Any]) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (changes, unchanged, errors)."""
    changes: list[dict] = []
    unchanged: list[dict] = []
    errors: list[dict] = []

    for source in SOURCES:
        if not source.get("enabled", True):
            continue

        key = source["key"]
        previous = (state.get("sdks") or {}).get(key, {})
        known_version = previous.get("version")

        try:
            raw = http_get(source["url"])
            version, published = PARSERS[source["parser"]](raw)
        except Exception as err:  # noqa: BLE001 - one bad registry must not kill the run
            errors.append({"source": source, "error": str(err)})
            print(f"ERROR  {source['label']}: {err}")
            continue

        if not version:
            errors.append({"source": source, "error": "no version found in response"})
            print(f"ERROR  {source['label']}: no version found in response")
            continue

        record = {
            "source": source,
            "version": version,
            "previous": known_version,
            "published": published,
            "is_new": known_version is not None and version != known_version,
            "is_upgrade": is_newer(version, known_version),
            "first_seen": known_version is None,
        }

        if record["is_new"]:
            changes.append(record)
            print(f"NEW    {source['label']}: {known_version} -> {version}")
        else:
            unchanged.append(record)
            marker = "SEED  " if record["first_seen"] else "OK    "
            print(f"{marker} {source['label']}: {version}")

    return changes, unchanged, errors


def build_new_state(
    records: list[dict], previous_state: dict[str, Any]
) -> dict[str, Any]:
    sdks = dict(previous_state.get("sdks") or {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for rec in records:
        key = rec["source"]["key"]
        existing = sdks.get(key, {})
        changed = rec["version"] != existing.get("version")
        sdks[key] = {
            "version": rec["version"],
            "published": rec["published"],
            "coordinate": rec["source"]["coordinate"],
            "first_detected": now if changed else existing.get("first_detected", now),
            "last_checked": now,
        }

    return {"last_run": now, "sdks": sdks}


# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #


def render_email(changes: list[dict], unchanged: list[dict], errors: list[dict]) -> tuple[str, str, str]:
    """Returns (subject, plain_text, html)."""
    labels = ", ".join(
        f"{c['source']['label']} {c['version']}" for c in changes
    ) or "status check"
    subject = f"[UXCam SDK] New release: {labels}" if changes else "[UXCam SDK] Status"

    # ---- plain text ----
    lines = ["UXCam SDK release watcher", ""]
    if changes:
        lines.append("NEW RELEASES")
        for c in changes:
            s = c["source"]
            lines.append(f"  - {s['label']} ({s['coordinate']})")
            lines.append(f"      {c['previous']}  ->  {c['version']}")
            if c["published"]:
                lines.append(f"      published: {c['published']}")
            lines.append(f"      upgrade:   {s['install_hint'].format(version=c['version'])}")
            lines.append(f"      changelog: {s['changelog']}")
            lines.append("")
    else:
        lines.append("No new releases detected.")
        lines.append("")

    if unchanged:
        lines.append("CURRENT VERSIONS")
        for u in unchanged:
            lines.append(f"  - {u['source']['label']}: {u['version']}")
        lines.append("")

    if errors:
        lines.append("CHECK FAILURES")
        for e in errors:
            lines.append(f"  - {e['source']['label']}: {e['error']}")
        lines.append("")

    lines.append("Action: raise an 'Upgrade UXCam SDK' task and schedule a regression pass")
    lines.append("over session recording, screen tagging, and PII occlusion.")
    text = "\n".join(lines)

    # ---- html ----
    def esc(value: str) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    blocks: list[str] = []
    if changes:
        rows = []
        for c in changes:
            s = c["source"]
            rows.append(
                f"""
        <tr>
          <td style="padding:14px 16px;border-bottom:1px solid #e6e8eb;">
            <div style="font-weight:600;color:#111827;">{esc(s['label'])}</div>
            <div style="font-size:12px;color:#6b7280;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">{esc(s['coordinate'])}</div>
          </td>
          <td style="padding:14px 16px;border-bottom:1px solid #e6e8eb;white-space:nowrap;">
            <span style="color:#6b7280;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">{esc(c['previous'])}</span>
            <span style="color:#9ca3af;">&rarr;</span>
            <span style="font-weight:700;color:#047857;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">{esc(c['version'])}</span>
          </td>
          <td style="padding:14px 16px;border-bottom:1px solid #e6e8eb;">
            <a href="{esc(s['changelog'])}" style="color:#2563eb;text-decoration:none;">Changelog</a>
          </td>
        </tr>"""
            )
        blocks.append(
            f"""
    <h2 style="font-size:15px;margin:24px 0 8px;color:#111827;">New releases</h2>
    <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;border:1px solid #e6e8eb;border-radius:8px;overflow:hidden;">
      {''.join(rows)}
    </table>
    <h3 style="font-size:13px;margin:20px 0 6px;color:#374151;">Upgrade lines</h3>
    <pre style="margin:0;padding:12px 14px;background:#f6f7f9;border:1px solid #e6e8eb;border-radius:6px;font-size:12px;color:#111827;overflow-x:auto;">{esc(chr(10).join(c['source']['install_hint'].format(version=c['version']) for c in changes))}</pre>"""
        )
    else:
        blocks.append(
            '<p style="color:#374151;">No new releases detected since the last check.</p>'
        )

    if unchanged:
        items = "".join(
            f'<li style="margin:2px 0;color:#374151;">{esc(u["source"]["label"])}: '
            f'<code style="color:#111827;">{esc(u["version"])}</code></li>'
            for u in unchanged
        )
        blocks.append(
            f'<h3 style="font-size:13px;margin:20px 0 6px;color:#374151;">Unchanged</h3>'
            f'<ul style="margin:0;padding-left:18px;font-size:13px;">{items}</ul>'
        )

    if errors:
        items = "".join(
            f'<li style="margin:2px 0;color:#b91c1c;">{esc(e["source"]["label"])}: {esc(e["error"])}</li>'
            for e in errors
        )
        blocks.append(
            f'<h3 style="font-size:13px;margin:20px 0 6px;color:#b91c1c;">Check failures</h3>'
            f'<ul style="margin:0;padding-left:18px;font-size:13px;">{items}</ul>'
        )

    html = f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:10px;padding:28px 30px;">
    <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#6b7280;">UXCam SDK watcher</div>
    <h1 style="font-size:19px;margin:6px 0 0;color:#111827;">{'New SDK release detected' if changes else 'No new SDK releases'}</h1>
    {''.join(blocks)}
    <p style="margin:24px 0 0;padding-top:16px;border-top:1px solid #e6e8eb;font-size:12px;color:#6b7280;">
      Next step: create the <strong>Upgrade UXCam SDK</strong> task and schedule a regression pass over
      session recording, screen tagging, and PII occlusion before release.
    </p>
  </div>
</body></html>"""

    return subject, text, html


def render_issue_body(changes: list[dict]) -> str:
    """Markdown body for the GitHub issue - the primary alert channel."""
    lines = [
        "A new UXCam SDK release was detected by the daily watcher.",
        "",
        "| Platform | Previous | New | Changelog |",
        "|---|---|---|---|",
    ]
    for c in changes:
        s = c["source"]
        lines.append(
            f"| {s['label']} | `{c['previous']}` | **`{c['version']}`** | "
            f"[Release notes]({s['changelog']}) |"
        )

    lines += ["", "### Upgrade", "", "```"]
    lines += [c["source"]["install_hint"].format(version=c["version"]) for c in changes]
    lines += ["```", ""]

    published = [c for c in changes if c.get("published")]
    if published:
        lines.append("<sub>Published: " + ", ".join(
            f"{c['source']['label']} {c['published']}" for c in published
        ) + "</sub>")
        lines.append("")

    lines += [
        "### QA checklist",
        "",
        "- [ ] Review the changelog for breaking changes and new privacy/occlusion APIs",
        "- [ ] Bump the dependency on a feature branch",
        "- [ ] Smoke test: session upload, screen tagging, user identity, custom events",
        "- [ ] Verify PII occlusion still applies to all sensitive fields",
        "- [ ] Check app size delta and cold-start impact",
        "- [ ] Confirm sessions appear in the UXCam dashboard from a real device build",
        "- [ ] Regression pass on the flows most dependent on session recording",
    ]
    return "\n".join(lines) + "\n"


class EmailNotConfigured(Exception):
    """Raised when SMTP settings are absent - not an error, just an unused channel."""


def send_email(subject: str, text: str, html: str) -> None:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("MAIL_FROM", user or "")
    recipients = [
        addr.strip()
        for addr in os.environ.get("MAIL_TO", "").split(",")
        if addr.strip()
    ]

    missing = [
        name
        for name, value in (
            ("SMTP_HOST", host),
            ("SMTP_USER", user),
            ("SMTP_PASSWORD", password),
            ("MAIL_TO", recipients),
        )
        if not value
    ]
    if missing:
        raise EmailNotConfigured(f"missing: {', '.join(missing)}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("UXCam SDK Watcher", sender))
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=HTTP_TIMEOUT) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=HTTP_TIMEOUT) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)

    print(f"Email sent to {', '.join(recipients)}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description="UXCam SDK release watcher")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="path to state file")
    parser.add_argument("--dry-run", action="store_true", help="no email, no state write")
    parser.add_argument("--force-notify", action="store_true", help="email even if nothing changed")
    parser.add_argument("--seed", action="store_true", help="write state without emailing")
    args = parser.parse_args()

    state_path = Path(args.state)
    state = load_state(state_path)
    first_run = not state.get("sdks")

    if first_run:
        print("No previous state found - seeding baseline, no alert will be sent.")

    changes, unchanged, errors = check_all(state)

    if not changes and not unchanged:
        print("All checks failed; leaving state untouched.")
        return 1

    should_notify = bool(changes) or args.force_notify
    if args.dry_run or args.seed:
        should_notify = False

    exit_code = 0

    if should_notify:
        subject, text, html = render_email(changes, unchanged, errors)
        try:
            send_email(subject, text, html)
        except EmailNotConfigured as err:
            # Expected when the GitHub issue is the alert channel. Not a failure.
            print(f"Email channel not configured ({err}); relying on the GitHub issue.")
            print("--- digest ---")
            print(text)
        except Exception as err:  # noqa: BLE001
            print(f"ERROR: failed to send email: {err}")
            print("--- message that would have been sent ---")
            print(text)
            exit_code = 2

    if not args.dry_run:
        save_state(state_path, build_new_state(changes + unchanged, state))
        print(f"State written to {state_path}")

    if changes:
        Path("issue_body.md").write_text(render_issue_body(changes), encoding="utf-8")

    # Surface the outcome to CI without failing the job on a normal "new release".
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changes else 'false'}\n")
            handle.write(
                "versions="
                + "; ".join(f"{c['source']['label']} {c['version']}" for c in changes)
                + "\n"
            )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
