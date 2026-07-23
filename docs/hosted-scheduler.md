# Hosted Epioxa Scheduler

This project is prepared for GitHub Actions so the tracker can run without the local Codex app or this computer being awake.

## What Runs

- `.github/workflows/epioxa-daily-tracker.yml` runs the tracker daily.
- GitHub cron uses UTC, so it schedules both 12:00 UTC and 13:00 UTC.
- `scripts/run_epioxa_hosted_update.py` only proceeds when the current New York hour is 8, which handles daylight saving time.
- The workflow also supports manual runs from the GitHub Actions tab.

## What It Updates

- `outputs/epioxa-clinics-monitor.db`
- `index.html`
- `outputs/epioxa-dashboard.html`
- monitor CSV/report files under `outputs/`

## Publishing

The workflow publishes the built `dist/` folder to GitHub Pages. The dashboard URL will be:

`https://<github-user-or-org>.github.io/<repo-name>/`

The current `chatgpt.site` dashboard is separate from GitHub Pages and will not update from GitHub Actions unless a separate Sites deployment credential/API path is added.
