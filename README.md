# UXCam SDK Watch

Daily check for new UXCam SDK releases, with an email alert and an auto-created
"Upgrade UXCam SDK" issue. Backup for the fact that UXCam publishes no release
notification of its own.

Stdlib-only Python — no dependencies to install or keep patched.

## Where the versions come from

UXCam has no release feed, webhook, or mailing list. The reliable signal is the
package registry each SDK actually publishes to — those are machine-readable and
update the moment a release goes live.

| Platform | Source polled | Human changelog |
|---|---|---|
| Android | `repo1.maven.org/maven2/com/uxcam/uxcam/maven-metadata.xml` | [Android changelog](https://developer.uxcam.com/docs/android-changelog) |
| iOS | `trunk.cocoapods.org/api/v1/pods/UXCam` | [iOS changelog](https://developer.uxcam.com/docs/ios-sdk-change-log) |
| React Native | `registry.npmjs.org/react-native-ux-cam/latest` | [GitHub releases](https://github.com/uxcam/react-native-ux-cam/releases) |

Flutter (`pub.dev/api/packages/flutter_uxcam`) and Cordova are pre-configured but
disabled — see *Adding a platform* below.

The doc-site changelogs are the place to read *what* changed; they are not
polled because they carry no stable machine-readable feed.

## Setup

1. Push this folder to a repo (public or private — Actions minutes are free on
   public repos, and this job takes seconds).
2. Add these repository secrets under **Settings → Secrets and variables →
   Actions**:

   | Secret | Example | Notes |
   |---|---|---|
   | `SMTP_HOST` | `smtp.office365.com` | `smtp.gmail.com` for Gmail |
   | `SMTP_PORT` | `587` | `465` switches the script to implicit SSL |
   | `SMTP_USER` | `you@company.com` | |
   | `SMTP_PASSWORD` | app password | **Not** your account password — generate an app password / mail-app credential |
   | `MAIL_FROM` | `you@company.com` | Optional, defaults to `SMTP_USER` |
   | `MAIL_TO` | `you@company.com,qa-team@company.com` | Comma-separated |

3. Run the workflow once manually (**Actions → UXCam SDK watch → Run workflow**).
   The first run seeds `state.json` with today's versions and sends nothing.
   Every run after that alerts only on a change.

The cron is `0 6 * * *` — 09:00 Riyadh. GitHub's scheduler can drift by a few
minutes to an hour under load; that is fine for a release watcher.

## What happens on a new release

1. Email lands with the old → new version, the exact upgrade line, and a link to
   the platform changelog.
2. A GitHub issue is opened titled `Upgrade UXCam SDK — <platform> <version>`
   with a QA regression checklist (session upload, screen tagging, PII occlusion,
   app size, cold start).
3. `state.json` is committed back, so the next run compares against the new
   baseline and does not re-alert.

If you'd rather the ticket land in Jira, replace the *Open upgrade issue* step
with a `curl` to `POST /rest/api/3/issue` using a Jira API token secret — the
version string is already exposed as `steps.check.outputs.versions`.

## Running it locally

```bash
python3 check_uxcam.py --dry-run      # check + print, no email, no state write
python3 check_uxcam.py --seed         # write the baseline, stay quiet
python3 check_uxcam.py --force-notify # email the current status on demand
python3 test_parsers.py               # offline checks for parsers and diff logic
```

Email config is read from the same environment variables locally:

```bash
export SMTP_HOST=smtp.office365.com SMTP_PORT=587 \
       SMTP_USER=you@company.com SMTP_PASSWORD='app-password' \
       MAIL_TO=you@company.com
python3 check_uxcam.py --force-notify
```

If SMTP is unreachable or unconfigured the script prints the message it would
have sent and exits `2` — the check itself still tells you the versions.

## Adding a platform

Open `check_uxcam.py`, find `SOURCES`, and flip `"enabled": True` on the Flutter
or Cordova entry. For a registry that isn't covered, add an entry and a parser
returning `(version, published_iso_or_None)`.

## Design notes

- **Pre-releases are ignored.** Anything matching `alpha|beta|rc|dev|snapshot|preview`
  is filtered out, so a beta drop doesn't page you.
- **One bad registry doesn't kill the run.** Failures are collected, reported in
  the email, and the other platforms still get compared.
- **State is a committed file**, not a cache — the history shows when each
  version was first detected, which is useful evidence for an audit trail.
- **Version comparison is numeric**, not string — `3.10.9` sorts above `3.9.1`.

## Known limits

- Registry publication is the trigger, so the alert fires when the artifact is
  live, which can be slightly ahead of the changelog page being updated.
- npm's `/latest` manifest carries no publish timestamp, so the React Native row
  shows the version change without a date. The diff is what matters.
- A version yanked from a registry would read as a downgrade; the script reports
  the change but flags `is_upgrade` false internally rather than alerting twice.
