import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
DST_8AM_CRON = "0 12 * * *"
STANDARD_8AM_CRON = "0 13 * * *"


def run(cmd, *, check=True):
    print(f"+ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if check and result.returncode:
        raise SystemExit(result.returncode)
    return result


def expected_8am_schedule(now_ny):
    """Return the UTC cron slot that maps to 8am in New York today."""
    offset = now_ny.utcoffset()
    if offset is None:
        return None
    offset_hours = offset.total_seconds() / 3600
    if offset_hours == -4:
        return DST_8AM_CRON
    if offset_hours == -5:
        return STANDARD_8AM_CRON
    return None


def should_run_now():
    """Avoid duplicate DST schedules when GitHub runs both 12:00 and 13:00 UTC."""
    if "--force" in sys.argv:
        return True
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    scheduled_slot = os.environ.get("GITHUB_EVENT_SCHEDULE", "").strip()
    if scheduled_slot:
        expected_slot = expected_8am_schedule(now_ny)
        if expected_slot:
            if scheduled_slot == expected_slot:
                print(
                    f"Running scheduled slot {scheduled_slot!r} for "
                    f"{now_ny:%Y-%m-%d} America/New_York."
                )
                return True
            print(
                f"Skipping: scheduled slot {scheduled_slot!r} is not the active "
                f"8am America/New_York slot ({expected_slot!r}) for "
                f"{now_ny:%Y-%m-%d}."
            )
            return False
        print("Running: unable to determine the active 8am New York schedule slot.")
        return True

    if now_ny.hour == 8:
        return True
    print(f"Skipping: current America/New_York hour is {now_ny.hour}, not 8.")
    return False


def main():
    if not should_run_now():
        return 0

    monitor = OUTPUTS / "epioxa_weekly_monitor.py"
    run([sys.executable, str(monitor), "--delay-seconds", "5"])

    dashboard = OUTPUTS / "epioxa-dashboard.html"
    if dashboard.exists():
        shutil.copyfile(dashboard, ROOT / "index.html")

    summary = OUTPUTS / "epioxa-monitor-summary.txt"
    if summary.exists():
        print(summary.read_text(encoding="utf-8"))

    changed = run(["git", "status", "--porcelain"], check=False).stdout.strip()
    if not changed:
        print("No dashboard or tracker changes to commit.")
        return 0

    run(["git", "config", "user.name", "github-actions[bot]"])
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run(
        [
            "git",
            "add",
            "index.html",
            "outputs/epioxa-clinics-monitor.db",
            "outputs/epioxa-dashboard.html",
            "outputs/epioxa-monitor-summary.txt",
            "outputs/epioxa-monitor-new-since-baseline.csv",
            "outputs/epioxa-monitor-tracked-clinics.csv",
        ]
    )
    for path in sorted(OUTPUTS.glob("epioxa-monitor-new-clinics-*.csv")):
        run(["git", "add", str(path.relative_to(ROOT))])
    for path in sorted(OUTPUTS.glob("epioxa-monitor-not-seen-latest-run-*.csv")):
        run(["git", "add", str(path.relative_to(ROOT))])

    staged = run(["git", "diff", "--cached", "--name-only"], check=False).stdout.strip()
    if not staged:
        print("No staged tracker changes to commit.")
        return 0

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    run(["git", "commit", "-m", f"Update Epioxa tracker data for {today}"])
    run(["git", "push"])

    print(json.dumps({"status": "updated", "date": today}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
