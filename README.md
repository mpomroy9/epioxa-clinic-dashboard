# Epioxa Clinic Tracker

This repository monitors the public Epioxa clinic finder, records historical
observations in SQLite, and publishes a static clinic buildout dashboard.

## Source Of Truth

GitHub `main` is the canonical source branch for tracker code and state.
Scheduled GitHub Actions runs update the database, reports, dashboard HTML, and
GitHub Pages deployment from that branch.

The `chatgpt.site` dashboard is a separately deployed mirror. A Sites release
must start from the current GitHub `main` commit, build that exact checkout, and
save/deploy a Sites version using the same commit SHA. Do not deploy Sites from
an older local or detached worktree.

## Local Setup

Python 3.12 and Node.js 22 are the supported runtimes. The tracker has no
third-party Python runtime dependencies.

```powershell
python -m unittest discover -s tests -v
npm run build
```

Run a full monitor update:

```powershell
python outputs/epioxa_weekly_monitor.py --delay-seconds 5
```

Run a limited API smoke test only against a disposable database:

```powershell
python outputs/epioxa_weekly_monitor.py `
  --db outputs/epioxa-monitor.smoke.db `
  --max-query-pairs 2 `
  --delay-seconds 0
```

## Safety Controls

A full pull must complete every configured city/type query pair and remain
within 85% to 125% of the previous live clinic count. Suspicious pulls are
recorded as failed and are not committed by the hosted workflow.

Broad searches that hit the Epioxa API's 50-result ceiling trigger additional
75-mile searches from spatially distributed historical clinic locations. A
run is rejected if any of those supplemental coverage searches also reaches
the ceiling, because that result set cannot be treated as exhaustive.

Use `--allow-suspicious-counts` only after manually validating a legitimate
large change in the Epioxa finder. The ratios can be configured with
`EPIOXA_MINIMUM_LIVE_RATIO` and `EPIOXA_MAXIMUM_LIVE_RATIO`.

Dashboard generation and the static build must both succeed before the hosted
runner commits or pushes tracker state.

Each successful run writes `outputs/epioxa-target-place-id-gaps.csv` for
facilities that still need a targeted-recheck mapping. Epioxa's public
coordinate endpoint can fill those mappings from coordinates already stored in
the monitor database:

```powershell
python outputs/epioxa_weekly_monitor.py `
  --refresh-target-places-only `
  --delay-seconds 1
```

The refresh checkpoints every ten additions and can be resumed after a rate
limit. It is intentionally separate from the daily critical path; use
`--refresh-target-places` on a full pull only when the coordinate endpoint is
known to be available.

## Data Maintenance

The SQLite database remains the historical source of truth for now. Generated
daily CSV files and binary database history will increase repository size over
time. A future migration should retain compact dashboard snapshots in Git and
store historical database backups as versioned workflow artifacts or in
durable object storage.
