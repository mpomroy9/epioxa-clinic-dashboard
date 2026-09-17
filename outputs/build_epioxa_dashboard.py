import csv
import html
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "epioxa-clinics-monitor.db"
OUT_PATH = ROOT / "epioxa-dashboard.html"
ASSET_WEEKLY_PATH = ROOT / "epioxa-buildout-by-week-asset-proxy.csv"
ASSET_DETAIL_PATH = ROOT / "epioxa-clinics-with-asset-date-crosscheck.csv"


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def week_start(value):
    dt = parse_dt(value)
    if not dt:
        return ""
    dt = dt.astimezone(timezone.utc)
    monday = dt - timedelta(days=dt.weekday())
    return monday.date().isoformat()


def run_query(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def int_value(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_key(*parts):
    joined = " ".join(str(part or "") for part in parts)
    return re.sub(r"[^a-z0-9]+", " ", joined.lower()).strip()


def load_facility_asset_estimates():
    if not ASSET_DETAIL_PATH.exists():
        return {}

    exact = {}
    loose = {}
    with ASSET_DETAIL_PATH.open(newline="", encoding="utf-8-sig") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            estimate = row.get("Earliest facility asset created date") or row.get(
                "Earliest any public asset created date", ""
            )
            if not estimate:
                continue
            item = {
                "photo_first_seen_estimate": estimate,
                "photo_estimate_basis": row.get("Cross-check assessment", ""),
                "photo_estimate_source": row.get("Date source", ""),
            }
            exact[
                normalize_key(row.get("Clinic"), row.get("Address"), row.get("City"), row.get("State"))
            ] = item
            loose.setdefault(normalize_key(row.get("Clinic"), row.get("City"), row.get("State")), []).append(item)

    for key, matches in loose.items():
        if len(matches) == 1:
            exact.setdefault(key, matches[0])
    return exact


def load_asset_weekly_estimates():
    if not ASSET_WEEKLY_PATH.exists():
        return []

    rows = []
    with ASSET_WEEKLY_PATH.open(newline="", encoding="utf-8-sig") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            week = row.get("Week starting", "")
            if not week:
                continue
            added = int_value(row.get("Facilities with first asset created"))
            rows.append(
                {
                    "week_starting": week,
                    "clinics_added": added,
                    "treatment_centers": int_value(row.get("Treatment centers")),
                    "detection_centers": int_value(row.get("Detection centers")),
                    "both_center_types": int_value(row.get("Both")),
                    "source": "Estimate",
                    "source_detail": "Clinic photo added date",
                    "is_estimate": True,
                    "cumulative_buildout": 0,
                }
            )
    return rows


def load_dashboard_data():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    latest_run = conn.execute(
        "SELECT id, run_at, total_facilities, new_facilities, status FROM runs WHERE status IN ('complete', 'baseline') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    latest_run_id = latest_run["id"] if latest_run else None
    baseline_run = conn.execute(
        "SELECT id FROM runs WHERE status = 'baseline' ORDER BY id LIMIT 1"
    ).fetchone()
    baseline_run_id = baseline_run["id"] if baseline_run else None

    latest_seen_ids = set()
    if latest_run_id:
        latest_seen_ids = {
            row["facility_id"]
            for row in conn.execute(
                "SELECT DISTINCT facility_id FROM observations WHERE run_id = ?", (latest_run_id,)
            )
        }

    facilities = run_query(
        conn,
        """
        SELECT id, name, address, city, state, zip, is_treatment_center,
               is_detection_center, phone, website, providers, first_seen_at,
               first_seen_run_id, last_seen_at, last_seen_run_id
        FROM facilities
        ORDER BY lower(name), lower(city), lower(state)
        """,
    )
    asset_estimates = load_facility_asset_estimates()
    for facility in facilities:
        facility["currently_live"] = facility["id"] in latest_seen_ids
        facility["is_baseline"] = facility["first_seen_run_id"] == baseline_run_id
        facility["center_type"] = (
            "Treatment + Detection"
            if facility["is_treatment_center"] and facility["is_detection_center"]
            else "Treatment"
            if facility["is_treatment_center"]
            else "Detection"
            if facility["is_detection_center"]
            else ""
        )
        estimate = asset_estimates.get(
            normalize_key(facility["name"], facility["address"], facility["city"], facility["state"])
        ) or asset_estimates.get(normalize_key(facility["name"], facility["city"], facility["state"]))
        facility["photo_first_seen_estimate"] = (
            estimate.get("photo_first_seen_estimate", "") if estimate else ""
        )
        facility["photo_estimate_basis"] = estimate.get("photo_estimate_basis", "") if estimate else ""
        facility["photo_estimate_source"] = estimate.get("photo_estimate_source", "") if estimate else ""

    runs = run_query(
        conn,
        """
        SELECT id, run_at, total_facilities, new_facilities, status, notes
        FROM runs
        ORDER BY id
        """,
    )

    weekly_map = {}
    for facility in facilities:
        wk = week_start(facility["first_seen_at"])
        if not wk:
            continue
        row = weekly_map.setdefault(
            wk,
            {
                "week_starting": wk,
                "baseline_added": 0,
                "new_after_baseline": 0,
                "new_treatment_centers": 0,
                "new_detection_centers": 0,
                "new_both_center_types": 0,
                "total_added": 0,
                "cumulative_tracked": 0,
            },
        )
        row["total_added"] += 1
        if facility["is_baseline"]:
            row["baseline_added"] += 1
        else:
            row["new_after_baseline"] += 1
            if facility["is_treatment_center"]:
                row["new_treatment_centers"] += 1
            if facility["is_detection_center"]:
                row["new_detection_centers"] += 1
            if facility["is_treatment_center"] and facility["is_detection_center"]:
                row["new_both_center_types"] += 1

    cumulative = 0
    weekly = []
    for wk in sorted(weekly_map):
        cumulative += weekly_map[wk]["total_added"]
        weekly_map[wk]["cumulative_tracked"] = cumulative
        weekly.append(weekly_map[wk])

    official_weeks = [
        {
            "week_starting": row["week_starting"],
            "clinics_added": row["new_after_baseline"],
            "treatment_centers": row["new_treatment_centers"],
            "detection_centers": row["new_detection_centers"],
            "both_center_types": row["new_both_center_types"],
            "source": "Official",
            "source_detail": "Tracker first-seen date",
            "is_estimate": False,
            "cumulative_buildout": 0,
        }
        for row in weekly
        if row["new_after_baseline"] > 0
    ]
    first_official_week = min((row["week_starting"] for row in official_weeks), default="")
    estimated_weeks = load_asset_weekly_estimates()
    if first_official_week:
        estimated_weeks = [row for row in estimated_weeks if row["week_starting"] < first_official_week]

    cumulative_buildout = 0
    weekly_buildout = []
    for row in sorted(estimated_weeks + official_weeks, key=lambda item: item["week_starting"]):
        cumulative_buildout += row["clinics_added"]
        row["cumulative_buildout"] = cumulative_buildout
        weekly_buildout.append(row)

    latest_run_at = latest_run["run_at"] if latest_run else ""
    latest_run_week_start = week_start(latest_run_at)
    latest_week = next(
        (row for row in weekly if row["week_starting"] == latest_run_week_start),
        {},
    )
    current_facilities = [f for f in facilities if f["currently_live"]]
    summary = {
        "last_run": latest_run_at,
        "latest_run_week_start": latest_run_week_start,
        "currently_live_latest_pull": len(latest_seen_ids),
        "total_tracked_historically": len(facilities),
        "new_this_run": latest_run["new_facilities"] if latest_run else 0,
        "new_this_week": latest_week.get("new_after_baseline", 0),
        "new_since_baseline": sum(1 for f in facilities if not f["is_baseline"]),
        "not_seen_latest_pull": sum(1 for f in facilities if not f["currently_live"]),
        "treatment_centers": sum(1 for f in facilities if f["is_treatment_center"]),
        "detection_centers": sum(1 for f in facilities if f["is_detection_center"]),
        "both_center_types": sum(
            1 for f in facilities if f["is_treatment_center"] and f["is_detection_center"]
        ),
        "current_treatment_centers": sum(1 for f in current_facilities if f["is_treatment_center"]),
        "current_detection_centers": sum(1 for f in current_facilities if f["is_detection_center"]),
        "current_both_center_types": sum(
            1 for f in current_facilities if f["is_treatment_center"] and f["is_detection_center"]
        ),
    }

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "summary": summary,
        "weekly": weekly,
        "weekly_buildout": weekly_buildout,
        "runs": runs,
        "facilities": facilities,
    }


def render_dashboard(data):
    payload = (
        json.dumps(data, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    generated = html.escape(data["generated_at"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Epioxa Clinic Buildout Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #5a6675;
      --line: #d8dde5;
      --panel: #ffffff;
      --surface: #f4f7f8;
      --teal: #0f766e;
      --blue: #2458a6;
      --rose: #b31365;
      --amber: #9a5b00;
      --green-bg: #e6f4ef;
      --blue-bg: #e9f0fb;
      --rose-bg: #f9e8f1;
      --amber-bg: #fff3db;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: var(--surface);
      color: var(--ink);
      font-size: 14px;
      line-height: 1.35;
    }}
    header {{
      padding: 22px 28px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      position: sticky;
      top: 0;
      z-index: 20;
    }}
    h1 {{
      margin: 0 0 4px;
      font-size: 24px;
      letter-spacing: 0;
    }}
    .subtle {{ color: var(--muted); }}
    .top-status {{
      text-align: right;
      display: grid;
      gap: 5px;
      min-width: 250px;
    }}
    .top-status .label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      font-weight: 700;
    }}
    .top-status .time {{
      font-size: 17px;
      font-weight: 700;
    }}
    .top-status .source {{
      color: var(--muted);
      font-size: 12px;
    }}
    main {{
      padding: 20px 28px 28px;
      display: grid;
      gap: 18px;
    }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .section-head {{
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}
    h2 {{
      margin: 0;
      font-size: 16px;
      letter-spacing: 0;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(140px, 1fr));
      border-bottom: 1px solid var(--line);
    }}
    .metric {{
      padding: 14px 16px;
      border-right: 1px solid var(--line);
      min-height: 82px;
    }}
    .metric:last-child {{ border-right: 0; }}
    .metric .value {{
      font-size: 28px;
      font-weight: 700;
      margin-bottom: 4px;
      letter-spacing: 0;
    }}
    .metric .label {{ color: var(--muted); }}
    table {{
      border-collapse: collapse;
      width: 100%;
      table-layout: fixed;
    }}
    th, td {{
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }}
    th {{
      background: #f7fafb;
      color: #364252;
      font-size: 12px;
      text-transform: uppercase;
      font-weight: 700;
      position: sticky;
      top: 0;
      z-index: 5;
    }}
    tbody tr:hover {{ background: #fbfdff; }}
    .summary-table th:first-child, .summary-table td:first-child {{ width: 34%; }}
    .weekly-note {{
      padding: 10px 16px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
      background: #fbfdff;
    }}
    .weekly-wrap {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 260px;
      gap: 0;
    }}
    .estimate-row td {{
      font-style: italic;
      color: #52606d;
    }}
    .basis {{
      font-weight: 700;
    }}
    .estimate-row .basis {{
      font-style: italic;
      color: var(--amber);
    }}
    .official-row .basis {{
      color: var(--teal);
    }}
    .weekly-chart {{
      border-left: 1px solid var(--line);
      padding: 14px 14px 10px;
      display: grid;
      gap: 10px;
      align-content: start;
      grid-template-rows: auto auto minmax(0, 1fr);
      max-height: 360px;
      overflow: hidden;
    }}
    .latest-week-card {{
      border: 1px solid var(--line);
      background: #f7fafb;
      border-radius: 8px;
      padding: 12px;
      display: grid;
      gap: 4px;
    }}
    .latest-week-card .kicker {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }}
    .latest-week-card .number {{
      color: var(--teal);
      font-size: 34px;
      line-height: 1;
      font-weight: 700;
    }}
    .latest-week-card .detail {{
      color: var(--muted);
      font-size: 12px;
    }}
    .chart-caption {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      margin-top: 2px;
    }}
    .bar-scroll {{
      min-height: 0;
      overflow-y: auto;
      display: grid;
      gap: 10px;
      padding-right: 4px;
    }}
    .bar-row {{
      display: grid;
      grid-template-columns: 76px 1fr 34px;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: var(--muted);
    }}
    .bar-track {{
      height: 10px;
      background: #e7ecef;
      border-radius: 999px;
      overflow: hidden;
    }}
    .bar-fill {{
      height: 100%;
      background: var(--blue);
    }}
    .bar-fill.estimated {{
      background: #8793a0;
    }}
    .controls {{
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      display: grid;
      grid-template-columns: minmax(220px, 1fr) 170px 150px 150px;
      gap: 10px;
      align-items: center;
    }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      background: white;
      color: var(--ink);
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
    }}
    .table-scroll {{
      max-height: 62vh;
      overflow: auto;
    }}
    .clinics-table th:nth-child(1) {{ width: 230px; }}
    .clinics-table th:nth-child(2) {{ width: 260px; }}
    .clinics-table th:nth-child(3) {{ width: 120px; }}
    .clinics-table th:nth-child(4) {{ width: 70px; }}
    .clinics-table th:nth-child(5) {{ width: 90px; }}
    .clinics-table th:nth-child(6) {{ width: 120px; }}
    .clinics-table th:nth-child(7) {{ width: 120px; }}
    .clinics-table th:nth-child(8) {{ width: 120px; }}
    .clinics-table th:nth-child(9) {{ width: 150px; }}
    .clinics-table th:nth-child(10) {{ width: 190px; }}
    .clinics-table th:nth-child(11) {{ width: 190px; }}
    .clinics-table th:nth-child(12) {{ width: 135px; }}
    .clinics-table th:nth-child(13) {{ width: 180px; }}
    .clinics-table th:nth-child(14) {{ width: 300px; }}
    .new-clinics-table th:nth-child(1) {{ width: 150px; }}
    .new-clinics-table th:nth-child(2) {{ width: 230px; }}
    .new-clinics-table th:nth-child(3) {{ width: 260px; }}
    .new-clinics-table th:nth-child(4) {{ width: 120px; }}
    .new-clinics-table th:nth-child(5) {{ width: 70px; }}
    .new-clinics-table th:nth-child(6) {{ width: 90px; }}
    .new-clinics-table th:nth-child(7) {{ width: 120px; }}
    .new-clinics-table th:nth-child(8) {{ width: 120px; }}
    .new-clinics-table th:nth-child(9) {{ width: 135px; }}
    .new-clinics-table th:nth-child(10) {{ width: 180px; }}
    .new-clinics-table th:nth-child(11) {{ width: 300px; }}
    .new-clinics-table th:nth-child(12) {{ width: 260px; }}
    .pill {{
      display: inline-flex;
      align-items: center;
      height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .yes {{ background: var(--green-bg); color: var(--teal); }}
    .no {{ background: #eef1f4; color: var(--muted); }}
    .new {{ background: var(--blue-bg); color: var(--blue); }}
    .warn {{ background: var(--amber-bg); color: var(--amber); }}
    a {{ color: var(--blue); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .footer-note {{
      color: var(--muted);
      font-size: 12px;
      padding: 0 2px 6px;
    }}
    .th-note {{
      display: block;
      margin-top: 2px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 400;
      text-transform: none;
    }}
    .photo-estimate-date {{
      font-style: italic;
    }}
    @media (max-width: 980px) {{
      header {{ align-items: flex-start; flex-direction: column; }}
      .summary-grid {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }}
      .weekly-wrap {{ grid-template-columns: 1fr; }}
      .weekly-chart {{ border-left: 0; border-top: 1px solid var(--line); }}
      .controls {{ grid-template-columns: 1fr 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Epioxa Clinic Buildout Dashboard</h1>
      <div class="subtle">Clinic buildout tracker generated from the local Epioxa monitor database</div>
    </div>
    <div class="top-status">
      <div class="label">Latest monitor run</div>
      <div class="time" id="topLastRun">Loading...</div>
      <div class="source">Source: find-a-doctor.epioxa.com public finder/API</div>
    </div>
  </header>

  <main>
    <section>
      <div class="section-head">
        <h2>Summary</h2>
        <span id="lastRun" class="subtle"></span>
      </div>
      <div class="summary-grid" id="summaryGrid"></div>
      <table class="summary-table">
        <thead>
          <tr><th>Measure</th><th>Count</th><th>Plain English</th></tr>
        </thead>
        <tbody id="summaryTable"></tbody>
      </table>
    </section>

    <section>
      <div class="section-head">
        <h2>Clinics Added By Week</h2>
        <span class="subtle">Estimate rows before tracker launch; official rows after</span>
      </div>
      <div class="weekly-note">
        <em>Estimated rows use each clinic's first detected clinic-photo added date as a proxy for when that location came online, and only include clinics with a usable photo date. Official rows use this dashboard's tracker first-seen date after the database was created.</em>
      </div>
      <div class="weekly-wrap">
        <div class="table-scroll" id="weeklyTableScroll" style="max-height: 360px;">
          <table>
            <thead>
              <tr>
                <th>Week Starting</th>
                <th>Clinics Added</th>
                <th>Treatment</th>
                <th>Detection</th>
                <th>Both</th>
                <th>Basis</th>
                <th>Cumulative In Timeline</th>
              </tr>
            </thead>
            <tbody id="weeklyTable"></tbody>
          </table>
        </div>
        <div class="weekly-chart" id="weeklyChart"></div>
      </div>
    </section>

    <section>
      <div class="section-head">
        <h2>New Clinics Added This Week</h2>
        <span id="newClinicCount" class="subtle"></span>
      </div>
      <div class="table-scroll" style="max-height: 430px;">
        <table class="new-clinics-table">
          <thead>
            <tr>
              <th>First Seen</th>
              <th>Clinic</th>
              <th>Address</th>
              <th>City</th>
              <th>State</th>
              <th>Zip</th>
              <th>Center Type</th>
              <th>Currently Live</th>
              <th>Phone</th>
              <th>Website</th>
              <th>Providers</th>
              <th>Epioxa Facility ID</th>
            </tr>
          </thead>
          <tbody id="newClinicTable"></tbody>
        </table>
      </div>
    </section>

    <section>
      <div class="section-head">
        <h2>All Clinics</h2>
        <span id="clinicCount" class="subtle"></span>
      </div>
      <div class="controls">
        <input id="searchBox" type="search" placeholder="Search clinic, city, state, provider, website">
        <select id="stateFilter"><option value="">All states</option></select>
        <select id="typeFilter">
          <option value="">All center types</option>
          <option value="Treatment">Treatment</option>
          <option value="Detection">Detection</option>
          <option value="Treatment + Detection">Treatment + Detection</option>
        </select>
        <select id="liveFilter">
          <option value="">All live statuses</option>
          <option value="true">Currently live</option>
          <option value="false">Not seen latest</option>
        </select>
      </div>
      <div class="table-scroll">
        <table class="clinics-table">
          <thead>
            <tr>
              <th>Clinic</th>
              <th>Address</th>
              <th>City</th>
              <th>State</th>
              <th>Zip</th>
              <th>Center Type</th>
              <th>Currently Live</th>
              <th>New Since Baseline</th>
              <th>First Seen<span class="th-note">italic = photo estimate</span></th>
              <th>Last Seen</th>
              <th>Phone</th>
              <th>Website</th>
              <th>Providers</th>
              <th>Epioxa Facility ID</th>
            </tr>
          </thead>
          <tbody id="clinicTable"></tbody>
        </table>
      </div>
    </section>

    <div class="footer-note">
      Italic First Seen dates use the first detected clinic-photo asset date. Non-italic dates use when this local monitor first observed the clinic. Neither is an official go-live date.
    </div>
  </main>

  <script id="dashboard-data" type="application/json">{payload}</script>
  <script>
    const data = JSON.parse(document.getElementById('dashboard-data').textContent);
    const summary = data.summary;
    const facilities = data.facilities;

    const fmt = value => new Intl.NumberFormat().format(value ?? 0);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
    const formatShortDate = value => {{
      if (!value) return '';
      const d = new Date(value);
      if (Number.isNaN(d.valueOf())) {{
        const parts = String(value).slice(0, 10).split('-');
        return parts.length === 3 ? `${{parts[1]}}/${{parts[2]}}/${{parts[0].slice(2)}}` : value;
      }}
      return new Intl.DateTimeFormat(undefined, {{
        month: '2-digit',
        day: '2-digit',
        year: '2-digit',
        timeZone: 'UTC'
      }}).format(d);
    }};
    const firstSeenDate = facility => formatShortDate(facility.photo_first_seen_estimate || facility.first_seen_at);
    const firstSeenClass = facility => facility.photo_first_seen_estimate ? ' class="photo-estimate-date"' : '';
    const shortDate = value => formatShortDate(value);
    const weekStart = value => {{
      if (!value) return '';
      const d = new Date(value);
      if (Number.isNaN(d.valueOf())) return '';
      const utc = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
      const day = utc.getUTCDay() || 7;
      utc.setUTCDate(utc.getUTCDate() - day + 1);
      return utc.toISOString().slice(0, 10);
    }};
    const displayDateTime = value => {{
      if (!value) return 'Not run yet';
      const d = new Date(value);
      if (Number.isNaN(d.valueOf())) return value;
      return d.toLocaleString(undefined, {{
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        timeZoneName: 'short'
      }});
    }};
    const boolPill = value => `<span class="pill ${{value ? 'yes' : 'no'}}">${{value ? 'Yes' : 'No'}}</span>`;

    document.getElementById('topLastRun').textContent = displayDateTime(summary.last_run);
    document.getElementById('lastRun').textContent = `Latest monitor run: ${{displayDateTime(summary.last_run)}}`;

    const metrics = [
      ['New clinics added this week', summary.new_this_week, 'new'],
      ['New since baseline', summary.new_since_baseline, 'new'],
      ['Currently live', summary.currently_live_latest_pull, 'yes']
    ];
    document.getElementById('summaryGrid').innerHTML = metrics.map(([label, value, cls]) => `
      <div class="metric">
        <div class="value">${{fmt(value)}}</div>
        <div class="label">${{esc(label)}}</div>
      </div>
    `).join('');

    const summaryRows = [
      ['New clinics added this week', summary.new_this_week, 'Clinics first seen this week that were not in the original baseline.'],
      ['New clinics since baseline', summary.new_since_baseline, 'Clinics not present in the original 398-clinic baseline.'],
      ['Currently live in latest pull', summary.currently_live_latest_pull, 'Clinics returned by the most recent nationwide Epioxa pull.'],
      ['Previously tracked but not seen latest', summary.not_seen_latest_pull, 'Clinics in the database that did not appear in the latest pull.'],
      ['Current live treatment centers', summary.current_treatment_centers, 'Currently live clinics marked as treatment centers.'],
      ['Current live detection centers', summary.current_detection_centers, 'Currently live clinics marked as detection centers.'],
      ['Current live both treatment and detection', summary.current_both_center_types, 'Overlap count. Current live math: treatment + detection - both = unique live clinics.']
    ];
    document.getElementById('summaryTable').innerHTML = summaryRows.map(([label, value, note]) => `
      <tr><td>${{esc(label)}}</td><td>${{fmt(value)}}</td><td>${{esc(note)}}</td></tr>
    `).join('');

    const weeklyBuildout = data.weekly_buildout || [];
    document.getElementById('weeklyTable').innerHTML = weeklyBuildout.map(row => `
      <tr class="${{row.is_estimate ? 'estimate-row' : 'official-row'}}">
        <td>${{esc(row.week_starting)}}</td>
        <td>${{fmt(row.clinics_added)}}</td>
        <td>${{fmt(row.treatment_centers)}}</td>
        <td>${{fmt(row.detection_centers)}}</td>
        <td>${{fmt(row.both_center_types)}}</td>
        <td><span class="basis">${{esc(row.source)}}</span><br><span class="subtle">${{esc(row.source_detail)}}</span></td>
        <td>${{fmt(row.cumulative_buildout)}}</td>
      </tr>
    `).join('');
    const weeklyTableScroll = document.getElementById('weeklyTableScroll');
    if (weeklyTableScroll) {{
      weeklyTableScroll.scrollTop = weeklyTableScroll.scrollHeight;
    }}

    const latestOfficialWeek = [...weeklyBuildout].reverse().find(row => !row.is_estimate);
    const chartRows = [...weeklyBuildout];
    const maxWeekly = Math.max(1, ...chartRows.map(row => row.clinics_added));
    const latestWeekCard = latestOfficialWeek ? `
      <div class="latest-week-card">
        <div class="kicker">Latest official week</div>
        <div class="number">${{fmt(latestOfficialWeek.clinics_added)}}</div>
        <div><strong>${{esc(latestOfficialWeek.week_starting)}}</strong></div>
        <div class="detail">${{fmt(latestOfficialWeek.treatment_centers)}} treatment / ${{fmt(latestOfficialWeek.detection_centers)}} detection / ${{fmt(latestOfficialWeek.both_center_types)}} both</div>
      </div>
      <div class="chart-caption">Weekly Trend</div>
    ` : '';
    document.getElementById('weeklyChart').innerHTML = latestWeekCard + `
      <div class="bar-scroll" id="weeklyChartBars">
    ` + chartRows.map(row => `
      <div class="bar-row">
        <span>${{esc(row.week_starting)}}${{row.is_estimate ? ' est.' : ''}}</span>
        <span class="bar-track"><span class="bar-fill ${{row.is_estimate ? 'estimated' : ''}}" style="width: ${{Math.max(4, Math.round(row.clinics_added / maxWeekly * 100))}}%"></span></span>
        <strong>${{fmt(row.clinics_added)}}</strong>
      </div>
    `).join('') + `</div>`;
    const weeklyChartBars = document.getElementById('weeklyChartBars');
    if (weeklyChartBars) {{
      weeklyChartBars.scrollTop = weeklyChartBars.scrollHeight;
    }}

    const latestRunWeekStart = summary.latest_run_week_start || weekStart(summary.last_run);
    const newClinics = facilities
      .filter(f => !f.is_baseline && weekStart(f.first_seen_at) === latestRunWeekStart)
      .sort((a, b) => String(b.first_seen_at).localeCompare(String(a.first_seen_at)) || String(a.name).localeCompare(String(b.name)));
    document.getElementById('newClinicCount').textContent = `${{fmt(newClinics.length)}} clinics first seen during week of ${{esc(latestRunWeekStart)}}`;
    document.getElementById('newClinicTable').innerHTML = newClinics.length ? newClinics.map(f => `
      <tr>
        <td${{firstSeenClass(f)}}>${{esc(firstSeenDate(f))}}</td>
        <td>${{esc(f.name)}}</td>
        <td>${{esc(f.address)}}</td>
        <td>${{esc(f.city)}}</td>
        <td>${{esc(f.state)}}</td>
        <td>${{esc(f.zip)}}</td>
        <td>${{esc(f.center_type)}}</td>
        <td>${{boolPill(f.currently_live)}}</td>
        <td>${{esc(f.phone)}}</td>
        <td>${{f.website ? `<a href="${{esc(f.website.startsWith('http') ? f.website : 'https://' + f.website)}}" target="_blank" rel="noreferrer">${{esc(f.website)}}</a>` : ''}}</td>
        <td>${{esc(f.providers)}}</td>
        <td>${{esc(f.id)}}</td>
      </tr>
    `).join('') : '<tr><td colspan="12" class="subtle">No clinics were first seen during this monitor week.</td></tr>';

    const states = [...new Set(facilities.map(f => f.state).filter(Boolean))].sort();
    document.getElementById('stateFilter').innerHTML += states.map(state => `<option value="${{esc(state)}}">${{esc(state)}}</option>`).join('');

    function renderClinics() {{
      const query = document.getElementById('searchBox').value.trim().toLowerCase();
      const state = document.getElementById('stateFilter').value;
      const type = document.getElementById('typeFilter').value;
      const live = document.getElementById('liveFilter').value;
      const filtered = facilities.filter(f => {{
        if (state && f.state !== state) return false;
        if (type && f.center_type !== type) return false;
        if (live && String(f.currently_live) !== live) return false;
        if (!query) return true;
        return [f.name, f.address, f.city, f.state, f.zip, f.website, f.providers, f.id].join(' ').toLowerCase().includes(query);
      }});
      document.getElementById('clinicCount').textContent = `${{fmt(filtered.length)}} of ${{fmt(facilities.length)}} clinics shown`;
      document.getElementById('clinicTable').innerHTML = filtered.map(f => `
        <tr>
          <td>${{esc(f.name)}}</td>
          <td>${{esc(f.address)}}</td>
          <td>${{esc(f.city)}}</td>
          <td>${{esc(f.state)}}</td>
          <td>${{esc(f.zip)}}</td>
          <td>${{esc(f.center_type)}}</td>
          <td>${{boolPill(f.currently_live)}}</td>
          <td>${{f.is_baseline ? '<span class="pill no">No</span>' : '<span class="pill new">Yes</span>'}}</td>
          <td${{firstSeenClass(f)}}>${{esc(firstSeenDate(f))}}</td>
          <td>${{esc(shortDate(f.last_seen_at))}}</td>
          <td>${{esc(f.phone)}}</td>
          <td>${{f.website ? `<a href="${{esc(f.website.startsWith('http') ? f.website : 'https://' + f.website)}}" target="_blank" rel="noreferrer">${{esc(f.website)}}</a>` : ''}}</td>
          <td>${{esc(f.providers)}}</td>
          <td>${{esc(f.id)}}</td>
        </tr>
      `).join('');
    }}

    ['searchBox', 'stateFilter', 'typeFilter', 'liveFilter'].forEach(id => {{
      document.getElementById(id).addEventListener('input', renderClinics);
      document.getElementById(id).addEventListener('change', renderClinics);
    }});
    renderClinics();
  </script>
</body>
</html>
"""


def main():
    data = load_dashboard_data()
    OUT_PATH.write_text(render_dashboard(data), encoding="utf-8")
    print(json.dumps({"dashboard": str(OUT_PATH), "clinics": len(data["facilities"])}, indent=2))


if __name__ == "__main__":
    main()
