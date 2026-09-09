#!/usr/bin/env python3
"""Offline checks for the registry parsers and change-detection logic.

Run: python3 test_parsers.py
"""

import json
import sys
import tempfile
from pathlib import Path

import check_uxcam as w

failures: list[str] = []


def check(name: str, actual, expected) -> None:
    if actual == expected:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}: got {actual!r}, expected {expected!r}")
        failures.append(name)


# --- Maven Central metadata (com.uxcam:uxcam) ------------------------------- #
MAVEN = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata>
  <groupId>com.uxcam</groupId>
  <artifactId>uxcam</artifactId>
  <versioning>
    <latest>3.10.9</latest>
    <release>3.10.9</release>
    <versions>
      <version>3.9.1</version>
      <version>3.10.0</version>
      <version>3.10.2-beta</version>
      <version>3.10.9</version>
    </versions>
    <lastUpdated>20260825073320</lastUpdated>
  </versioning>
</metadata>"""

version, published = w.parse_maven(MAVEN)
check("maven picks highest stable version", version, "3.10.9")
check("maven ignores pre-release", w.parse_maven(
    MAVEN.replace(b"<version>3.10.9</version>", b"<version>3.11.0-rc1</version>")
)[0], "3.10.0")
check("maven parses lastUpdated", published, "2026-08-25T07:33:20+00:00")

# --- CocoaPods trunk (UXCam) ------------------------------------------------ #
COCOAPODS = json.dumps({
    "name": "UXCam",
    "versions": [
        {"name": "3.9.5", "created_at": "2026-05-02 09:11:00 UTC"},
        {"name": "3.10.0", "created_at": "2026-07-14 10:00:00 UTC"},
        {"name": "3.10.3", "created_at": "2026-09-01 11:02:47 UTC"},
    ],
}).encode()

version, published = w.parse_cocoapods(COCOAPODS)
check("cocoapods latest version", version, "3.10.3")
check("cocoapods created_at", published, "2026-09-01 11:02:47 UTC")

# --- npm /latest manifest --------------------------------------------------- #
NPM = json.dumps({"name": "react-native-ux-cam", "version": "6.0.22"}).encode()
check("npm latest version", w.parse_npm(NPM)[0], "6.0.22")

# --- pub.dev ---------------------------------------------------------------- #
PUB = json.dumps({
    "name": "flutter_uxcam",
    "latest": {"version": "2.10.0", "published": "2026-09-01T12:22:43.000Z"},
}).encode()
check("pubdev latest version", w.parse_pubdev(PUB)[0], "2.10.0")

# --- version comparison ----------------------------------------------------- #
check("3.10.9 > 3.9.1", w.is_newer("3.10.9", "3.9.1"), True)
check("3.10.9 > 3.10.10 is False", w.is_newer("3.10.9", "3.10.10"), False)
check("equal is not newer", w.is_newer("3.10.9", "3.10.9"), False)
check("no prior version is not newer", w.is_newer("3.10.9", None), False)
check("beta flagged as unstable", w.is_stable("3.11.0-beta1"), False)
check("plain version is stable", w.is_stable("3.11.0"), True)

# --- state round-trip and change detection ---------------------------------- #
source = w.SOURCES[0]
records = [{
    "source": source,
    "version": "3.10.9",
    "previous": "3.10.8",
    "published": None,
    "is_new": True,
    "is_upgrade": True,
    "first_seen": False,
}]
new_state = w.build_new_state(records, {})
check("state stores version", new_state["sdks"]["android"]["version"], "3.10.9")

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "state.json"
    w.save_state(path, new_state)
    check("state round-trips", w.load_state(path)["sdks"]["android"]["version"], "3.10.9")

# --- email rendering -------------------------------------------------------- #
subject, text, html = w.render_email(records, [], [])
check("subject flags the new release", "3.10.9" in subject, True)
check("body carries the upgrade line", "com.uxcam:uxcam:3.10.9" in text, True)
check("html includes changelog link", source["changelog"] in html, True)

subject, text, _ = w.render_email([], [{"source": source, "version": "3.10.9"}], [])
check("quiet run subject", subject, "[UXCam SDK] Status")
check("quiet run body", "No new releases detected." in text, True)

print()
if failures:
    print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")
