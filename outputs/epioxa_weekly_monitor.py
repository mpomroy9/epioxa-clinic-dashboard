import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DEFAULT_BASELINE = ROOT / "epioxa-clinics.json"
DEFAULT_DB = ROOT / "epioxa-clinics-monitor.db"
DEFAULT_TARGET_PLACES = ROOT / "epioxa-target-place-ids.json"
API_SEARCH = "https://api.find-a-doctor.epioxa.com/search/"
SOURCE = "https://find-a-doctor.epioxa.com/"
CLINIC_TYPES = ("treatment", "detection")
DEFAULT_MINIMUM_LIVE_RATIO = 0.85
DEFAULT_MAXIMUM_LIVE_RATIO = 1.25


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path):
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def address_text(address):
    if not address:
        return ""
    parts = [
        address.get("line1"),
        address.get("line2"),
        f"{address.get('city', '')}, {address.get('state', '')} {address.get('zipCode', '')}".strip(),
    ]
    return ", ".join([p for p in parts if p])


def providers_text(facility):
    return "; ".join(p.get("name", "") for p in facility.get("providers", []) if p.get("name"))


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            source TEXT NOT NULL,
            total_facilities INTEGER NOT NULL DEFAULT 0,
            new_facilities INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS facilities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            address TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            is_treatment_center INTEGER NOT NULL DEFAULT 0,
            is_detection_center INTEGER NOT NULL DEFAULT 0,
            phone TEXT,
            website TEXT,
            providers TEXT,
            first_seen_at TEXT NOT NULL,
            first_seen_run_id INTEGER,
            last_seen_at TEXT NOT NULL,
            last_seen_run_id INTEGER,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS observations (
            run_id INTEGER NOT NULL,
            facility_id TEXT NOT NULL,
            seen_at TEXT NOT NULL,
            name TEXT NOT NULL,
            city TEXT,
            state TEXT,
            clinic_type_query TEXT,
            PRIMARY KEY (run_id, facility_id, clinic_type_query)
        );

        CREATE TABLE IF NOT EXISTS place_queries (
            query TEXT NOT NULL,
            matched_text TEXT,
            place_id TEXT NOT NULL,
            clinic_type TEXT NOT NULL,
            last_count INTEGER,
            last_status TEXT,
            last_checked_at TEXT,
            PRIMARY KEY (place_id, clinic_type)
        );
        """
    )
    conn.commit()


def insert_run(conn, status="running", notes=""):
    now = utc_now()
    cur = conn.execute(
        "INSERT INTO runs (run_at, source, status, notes) VALUES (?, ?, ?, ?)",
        (now, SOURCE, status, notes),
    )
    conn.commit()
    return cur.lastrowid, now


def update_run(conn, run_id, total, new_count, status, notes="", commit=True):
    conn.execute(
        """
        UPDATE runs
        SET total_facilities = ?, new_facilities = ?, status = ?, notes = ?
        WHERE id = ?
        """,
        (total, new_count, status, notes, run_id),
    )
    if commit:
        conn.commit()


def get_baseline_run_id(conn):
    row = conn.execute(
        "SELECT id FROM runs WHERE status = 'baseline' ORDER BY id LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def normalize_facility(facility):
    address = facility.get("address") or {}
    contact = facility.get("contact") or {}
    facility_type = facility.get("facilityType") or {}
    return {
        "id": facility.get("id"),
        "name": facility.get("name") or "",
        "address": address_text(address),
        "city": address.get("city") or "",
        "state": address.get("state") or "",
        "zip": address.get("zipCode") or "",
        "is_treatment_center": 1 if facility_type.get("isTreatmentCenter") else 0,
        "is_detection_center": 1 if facility_type.get("isDetectionCenter") else 0,
        "phone": contact.get("phone") or "",
        "website": contact.get("website") or "",
        "providers": providers_text(facility),
        "raw_json": json.dumps(facility, sort_keys=True),
    }


def upsert_facility(conn, facility, run_id, seen_at, baseline=False):
    row = normalize_facility(facility)
    existing = conn.execute("SELECT id FROM facilities WHERE id = ?", (row["id"],)).fetchone()
    is_new = existing is None
    first_seen_at = seen_at
    first_seen_run_id = run_id

    if is_new:
        conn.execute(
            """
            INSERT INTO facilities (
                id, name, address, city, state, zip, is_treatment_center,
                is_detection_center, phone, website, providers, first_seen_at,
                first_seen_run_id, last_seen_at, last_seen_run_id, raw_json
            ) VALUES (
                :id, :name, :address, :city, :state, :zip, :is_treatment_center,
                :is_detection_center, :phone, :website, :providers, :first_seen_at,
                :first_seen_run_id, :last_seen_at, :last_seen_run_id, :raw_json
            )
            """,
            {
                **row,
                "first_seen_at": first_seen_at,
                "first_seen_run_id": first_seen_run_id,
                "last_seen_at": seen_at,
                "last_seen_run_id": run_id,
            },
        )
    else:
        conn.execute(
            """
            UPDATE facilities
            SET name = :name,
                address = :address,
                city = :city,
                state = :state,
                zip = :zip,
                is_treatment_center = :is_treatment_center,
                is_detection_center = :is_detection_center,
                phone = :phone,
                website = :website,
                providers = :providers,
                last_seen_at = :last_seen_at,
                last_seen_run_id = :last_seen_run_id,
                raw_json = :raw_json
            WHERE id = :id
            """,
            {**row, "last_seen_at": seen_at, "last_seen_run_id": run_id},
        )
    return is_new and not baseline


def seed_database(conn, baseline_path):
    data = read_json(baseline_path)
    init_db(conn)

    count = conn.execute("SELECT COUNT(*) FROM facilities").fetchone()[0]
    if count:
        return False, count

    run_id, run_at = insert_run(conn, status="baseline", notes="Seeded from original Epioxa clinic pull.")
    baseline_at = data.get("pulledAt") or run_at
    for facility in data.get("facilities", []):
        upsert_facility(conn, facility, run_id, baseline_at, baseline=True)
    update_run(
        conn,
        run_id,
        total=len(data.get("facilities", [])),
        new_count=0,
        status="baseline",
        notes="Original clinic list loaded as baseline. first_seen_at is the baseline pull date, not go-live date.",
    )
    return True, len(data.get("facilities", []))


def fetch_json(url, timeout=45):
    req = Request(url, headers={"User-Agent": "Codex Epioxa clinic monitor"})
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def query_epioxa_url(url, label, retries=4) -> dict:
    for attempt in range(1, retries + 1):
        try:
            body = fetch_json(url)
            if not isinstance(body, dict) or not isinstance(body.get("facilities"), list):
                raise ValueError("Epioxa response did not contain a facilities list")
            return body
        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if retryable and attempt < retries:
                wait = 60 * attempt if exc.code == 429 else 10 * attempt
                print(
                    f"HTTP {exc.code} on {label}; waiting {wait}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            raise
        except (URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            if attempt < retries:
                wait = 10 * attempt
                print(
                    f"Transient response failure on {label}: {exc!r}; waiting {wait}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"No Epioxa request attempts were made for {label}")


def query_epioxa(place, clinic_type, retries=4) -> dict:
    params = {
        "latitude": "0",
        "longitude": "0",
        "placeId": place["placeId"],
        "distance": "500",
        "clinicType": clinic_type,
    }
    url = f"{API_SEARCH}?{urlencode(params)}"
    return query_epioxa_url(url, f"{place['query']} / {clinic_type}", retries)


def query_epioxa_by_place_id(place_id, clinic_type, distance, retries=4) -> dict:
    params = {
        "latitude": "0",
        "longitude": "0",
        "placeId": place_id,
        "distance": str(distance),
        "clinicType": clinic_type,
    }
    url = f"{API_SEARCH}?{urlencode(params)}"
    return query_epioxa_url(url, f"targeted {distance}mi / {clinic_type}", retries)


def load_target_places(path):
    path = Path(path)
    if not path.exists():
        return {}
    data = read_json(path)
    return data.get("byFacilityId", {})


def targeted_recheck_missing(conn, current_facilities, target_places, delay_seconds):
    if not target_places:
        return [], []

    existing = conn.execute(
        """
        SELECT id, name, is_treatment_center, is_detection_center
        FROM facilities
        ORDER BY lower(name)
        """
    ).fetchall()
    missed = [row for row in existing if row[0] not in current_facilities]
    found = []
    notes = []

    for facility_id, name, is_treatment, is_detection in missed:
        target = target_places.get(facility_id) or {}
        place_id = target.get("placeId")
        if not place_id:
            notes.append(f"targeted missing {name}: no cached placeId")
            continue

        clinic_types = []
        if is_treatment:
            clinic_types.append("treatment")
        if is_detection:
            clinic_types.append("detection")
        if not clinic_types:
            clinic_types = list(CLINIC_TYPES)

        was_found = False
        for clinic_type in clinic_types:
            for distance in (10, 25, 50, 75):
                body = query_epioxa_by_place_id(place_id, clinic_type, distance)
                match = next((f for f in body.get("facilities", []) if f.get("id") == facility_id), None)
                notes.append(
                    f"targeted {name} {clinic_type} {distance}mi: "
                    f"{len(body.get('facilities', []))} results, found={bool(match)}"
                )
                if match:
                    current_facilities[facility_id] = match
                    found.append(facility_id)
                    was_found = True
                    break
                if delay_seconds:
                    time.sleep(delay_seconds)
            if was_found:
                break

    return found, notes


def collect_current_facilities(
    conn,
    baseline_path,
    delay_seconds,
    max_query_pairs=None,
    target_places_path=DEFAULT_TARGET_PLACES,
):
    data = read_json(baseline_path)
    place_rows = [p for p in data.get("placeRows", []) if p.get("placeId")]
    facilities = {}
    query_notes = []
    query_results = []
    checked_at = utc_now()
    expected_query_pairs = len(place_rows) * len(CLINIC_TYPES)

    query_pairs = 0
    for place in place_rows:
        for clinic_type in CLINIC_TYPES:
            if max_query_pairs is not None and query_pairs >= max_query_pairs:
                return facilities, "; ".join(query_notes), query_results, expected_query_pairs
            body = query_epioxa(place, clinic_type)
            query_pairs += 1
            current = body.get("facilities", [])
            query_notes.append(f"{place.get('query')} {clinic_type}: {len(current)}")
            query_results.append(
                (
                    place.get("query", ""),
                    place.get("text", ""),
                    place["placeId"],
                    clinic_type,
                    len(current),
                    "ok",
                    checked_at,
                )
            )
            for facility in current:
                facilities[facility["id"]] = facility
            if delay_seconds:
                time.sleep(delay_seconds)

    if max_query_pairs is None:
        target_places = load_target_places(target_places_path)
        found_targeted, target_notes = targeted_recheck_missing(conn, facilities, target_places, delay_seconds)
        query_notes.append(f"targeted recheck recovered {len(found_targeted)} historically tracked clinics")
        query_notes.extend(target_notes)

    return facilities, "; ".join(query_notes), query_results, expected_query_pairs


def record_place_queries(conn, query_results):
    conn.executemany(
        """
        INSERT INTO place_queries (
            query, matched_text, place_id, clinic_type, last_count, last_status, last_checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(place_id, clinic_type) DO UPDATE SET
            query = excluded.query,
            matched_text = excluded.matched_text,
            last_count = excluded.last_count,
            last_status = excluded.last_status,
            last_checked_at = excluded.last_checked_at
        """,
        query_results,
    )


def validate_collection(
    current_count,
    query_count,
    expected_query_count,
    previous_count,
    minimum_live_ratio=DEFAULT_MINIMUM_LIVE_RATIO,
    maximum_live_ratio=DEFAULT_MAXIMUM_LIVE_RATIO,
):
    errors = []
    if query_count != expected_query_count:
        errors.append(
            f"completed {query_count} of {expected_query_count} expected query pairs"
        )
    if current_count <= 0:
        errors.append("the current pull returned no facilities")
    if previous_count:
        ratio = current_count / previous_count
        if ratio < minimum_live_ratio:
            errors.append(
                f"live count ratio {ratio:.3f} is below minimum {minimum_live_ratio:.3f} "
                f"({current_count} current vs {previous_count} previous)"
            )
        if ratio > maximum_live_ratio:
            errors.append(
                f"live count ratio {ratio:.3f} exceeds maximum {maximum_live_ratio:.3f} "
                f"({current_count} current vs {previous_count} previous)"
            )
    if errors:
        raise RuntimeError("Suspicious Epioxa pull rejected: " + "; ".join(errors))


def write_csv(path, rows):
    fieldnames = [
        "Clinic",
        "Address",
        "City",
        "State",
        "Zip",
        "Treatment center",
        "Detection center",
        "Currently live in latest run",
        "First seen by monitor",
        "Last seen by monitor",
        "Phone",
        "Website",
        "Providers",
        "Epioxa facility ID",
    ]
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export_target_place_gaps(conn, target_places_path, run_id):
    target_places = load_target_places(target_places_path)
    latest_seen_ids = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT facility_id FROM observations WHERE run_id = ?",
            (run_id,),
        )
    }
    rows = []
    for row in conn.execute(
        """
        SELECT id, name, address, city, state, zip, first_seen_at
        FROM facilities
        ORDER BY lower(name), lower(city), lower(state)
        """
    ):
        if row[0] in target_places:
            continue
        rows.append(
            {
                "Clinic": row[1],
                "Address": row[2],
                "City": row[3],
                "State": row[4],
                "Zip": row[5],
                "First seen by monitor": row[6],
                "Currently live in latest run": "Yes" if row[0] in latest_seen_ids else "No",
                "Epioxa facility ID": row[0],
            }
        )

    path = ROOT / "epioxa-target-place-id-gaps.csv"
    fieldnames = [
        "Clinic",
        "Address",
        "City",
        "State",
        "Zip",
        "First seen by monitor",
        "Currently live in latest run",
        "Epioxa facility ID",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path, len(rows)


def export_reports(conn, run_id, run_at, target_gap_count=None):
    stamp = run_at[:10]
    latest_seen_ids = {
        row[0]
        for row in conn.execute("SELECT DISTINCT facility_id FROM observations WHERE run_id = ?", (run_id,))
    }
    all_rows = []
    for row in conn.execute(
        """
        SELECT id, name, address, city, state, zip, is_treatment_center,
               is_detection_center, first_seen_at, first_seen_run_id, last_seen_at, phone, website, providers
        FROM facilities
        ORDER BY lower(name), lower(city), lower(state)
        """
    ):
        all_rows.append(
            {
                "Clinic": row[1],
                "Address": row[2],
                "City": row[3],
                "State": row[4],
                "Zip": row[5],
                "Treatment center": "Yes" if row[6] else "No",
                "Detection center": "Yes" if row[7] else "No",
                "Currently live in latest run": "Yes" if row[0] in latest_seen_ids else "No",
                "First seen by monitor": row[8],
                "_first_seen_run_id": row[9],
                "Last seen by monitor": row[10],
                "Phone": row[11],
                "Website": row[12],
                "Providers": row[13],
                "Epioxa facility ID": row[0],
            }
        )

    baseline_run_id = get_baseline_run_id(conn)
    new_rows = [r for r in all_rows if r["_first_seen_run_id"] == run_id]
    new_since_baseline_rows = [
        r
        for r in all_rows
        if r["_first_seen_run_id"] and r["_first_seen_run_id"] != baseline_run_id
    ]
    not_seen_rows = [r for r in all_rows if r["Currently live in latest run"] == "No"]

    current_path = ROOT / "epioxa-monitor-tracked-clinics.csv"
    new_path = ROOT / f"epioxa-monitor-new-clinics-{stamp}.csv"
    new_since_baseline_path = ROOT / "epioxa-monitor-new-since-baseline.csv"
    not_seen_path = ROOT / f"epioxa-monitor-not-seen-latest-run-{stamp}.csv"
    write_csv(current_path, all_rows)
    write_csv(new_path, new_rows)
    write_csv(new_since_baseline_path, new_since_baseline_rows)
    write_csv(not_seen_path, not_seen_rows)

    summary_path = ROOT / "epioxa-monitor-summary.txt"
    total = len(all_rows)
    current_live = len(latest_seen_ids)
    runs = conn.execute(
        "SELECT run_at, total_facilities, new_facilities, status FROM runs ORDER BY id DESC LIMIT 10"
    ).fetchall()
    lines = [
        "Epioxa weekly clinic monitor",
        f"Last run: {run_at}",
        f"Currently live in latest pull: {current_live}",
        f"Total tracked clinics historically: {total}",
        f"New clinics this run: {len(new_rows)}",
        f"New clinics since baseline: {len(new_since_baseline_rows)}",
        f"Previously tracked clinics not seen in latest pull: {len(not_seen_rows)}",
    ]
    if target_gap_count is not None:
        lines.append(f"Clinics missing targeted place IDs: {target_gap_count}")
    lines.extend(["", "Recent runs:"])
    for run in runs:
        lines.append(f"- {run[0]} | total {run[1]} | new {run[2]} | {run[3]}")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return current_path, new_path, new_since_baseline_path, not_seen_path, summary_path, len(new_rows), total


def rebuild_dashboard():
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    import build_epioxa_dashboard

    build_epioxa_dashboard.main()


def run_monitor(args):
    conn = sqlite3.connect(args.db)
    init_db(conn)
    seeded, baseline_count = seed_database(conn, args.baseline)
    if args.seed_only:
        print(json.dumps({"seeded": seeded, "baseline_count": baseline_count}, indent=2))
        return 0

    run_id, run_at = insert_run(conn)
    try:
        current, notes, query_results, expected_query_pairs = collect_current_facilities(
            conn,
            args.baseline,
            args.delay_seconds,
            args.max_query_pairs,
            args.target_places,
        )
        if args.max_query_pairs is not None:
            record_place_queries(conn, query_results)
            update_run(
                conn,
                run_id,
                len(current),
                0,
                "smoke_test",
                f"Completed {len(query_results)} query pairs without changing facilities.",
            )
            print(
                json.dumps(
                    {
                        "status": "smoke_test",
                        "run_at": run_at,
                        "query_pairs": len(query_results),
                        "facilities_returned": len(current),
                    },
                    indent=2,
                )
            )
            return 0

        previous_row = conn.execute(
            """
            SELECT total_facilities
            FROM runs
            WHERE status IN ('complete', 'baseline') AND id != ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        previous_count = previous_row[0] if previous_row else 0
        validate_collection(
            len(current),
            len(query_results),
            expected_query_pairs,
            previous_count,
            0 if args.allow_suspicious_counts else args.minimum_live_ratio,
            float("inf")
            if args.allow_suspicious_counts
            else args.maximum_live_ratio,
        )

        conn.execute("BEGIN")
        record_place_queries(conn, query_results)
        new_count = 0
        for facility in current.values():
            if upsert_facility(conn, facility, run_id, run_at):
                new_count += 1
            conn.execute(
                """
                INSERT OR IGNORE INTO observations (
                    run_id, facility_id, seen_at, name, city, state, clinic_type_query
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    facility.get("id"),
                    run_at,
                    facility.get("name", ""),
                    (facility.get("address") or {}).get("city", ""),
                    (facility.get("address") or {}).get("state", ""),
                    ",".join(facility.get("seenAs", [])),
                ),
            )
        update_run(
            conn,
            run_id,
            len(current),
            new_count,
            "complete",
            notes[:10000],
            commit=False,
        )
        conn.commit()
        target_gaps_path, target_gap_count = export_target_place_gaps(
            conn, args.target_places, run_id
        )
        current_path, new_path, new_since_baseline_path, not_seen_path, summary_path, report_new, total = export_reports(
            conn, run_id, run_at, target_gap_count
        )
        rebuild_dashboard()
        print(
            json.dumps(
                {
                    "status": "complete",
                    "run_at": run_at,
                    "total_current_pull": len(current),
                    "total_tracked": total,
                    "new_clinics": report_new,
                    "tracked_clinics_csv": str(current_path),
                    "new_clinics_csv": str(new_path),
                    "new_since_baseline_csv": str(new_since_baseline_path),
                    "not_seen_latest_run_csv": str(not_seen_path),
                    "target_place_id_gaps_csv": str(target_gaps_path),
                    "target_place_id_gaps": target_gap_count,
                    "summary": str(summary_path),
                },
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        conn.rollback()
        update_run(conn, run_id, 0, 0, "failed", repr(exc))
        raise


def main():
    parser = argparse.ArgumentParser(description="Monitor Epioxa clinic listings for newly live facilities.")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE), help="Baseline JSON from the original Epioxa pull.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument("--target-places", default=str(DEFAULT_TARGET_PLACES), help="Cached facility-id to Google place-id mapping for targeted rechecks.")
    parser.add_argument("--delay-seconds", type=float, default=float(os.environ.get("EPIOXA_DELAY_SECONDS", "5")))
    parser.add_argument("--max-query-pairs", type=int, help="Optional smoke-test limit for city/type API query pairs.")
    parser.add_argument(
        "--minimum-live-ratio",
        type=float,
        default=float(os.environ.get("EPIOXA_MINIMUM_LIVE_RATIO", DEFAULT_MINIMUM_LIVE_RATIO)),
        help="Reject a full pull below this ratio of the previous live count.",
    )
    parser.add_argument(
        "--maximum-live-ratio",
        type=float,
        default=float(os.environ.get("EPIOXA_MAXIMUM_LIVE_RATIO", DEFAULT_MAXIMUM_LIVE_RATIO)),
        help="Reject a full pull above this ratio of the previous live count.",
    )
    parser.add_argument(
        "--allow-suspicious-counts",
        action="store_true",
        help="Bypass live-count guardrails for a manually reviewed run.",
    )
    parser.add_argument("--export-only-run-id", type=int, help="Rewrite reports for an existing run ID without querying Epioxa.")
    parser.add_argument("--seed-only", action="store_true", help="Create and seed the database, but do not query Epioxa.")
    args = parser.parse_args()
    if (
        args.max_query_pairs is not None
        and Path(args.db).resolve() == DEFAULT_DB.resolve()
    ):
        parser.error("--max-query-pairs requires --db pointing to a non-production database")
    if args.export_only_run_id:
        conn = sqlite3.connect(args.db)
        init_db(conn)
        row = conn.execute("SELECT run_at FROM runs WHERE id = ?", (args.export_only_run_id,)).fetchone()
        if not row:
            raise SystemExit(f"No run found with id {args.export_only_run_id}")
        target_gaps_path, target_gap_count = export_target_place_gaps(
            conn, args.target_places, args.export_only_run_id
        )
        current_path, new_path, new_since_baseline_path, not_seen_path, summary_path, report_new, total = export_reports(
            conn, args.export_only_run_id, row[0], target_gap_count
        )
        rebuild_dashboard()
        print(
            json.dumps(
                {
                    "status": "exported",
                    "run_id": args.export_only_run_id,
                    "tracked_clinics_csv": str(current_path),
                    "new_clinics_csv": str(new_path),
                    "new_since_baseline_csv": str(new_since_baseline_path),
                    "not_seen_latest_run_csv": str(not_seen_path),
                    "target_place_id_gaps_csv": str(target_gaps_path),
                    "target_place_id_gaps": target_gap_count,
                    "summary": str(summary_path),
                    "new_clinics": report_new,
                    "total_tracked": total,
                },
                indent=2,
            )
        )
        return 0
    return run_monitor(args)


if __name__ == "__main__":
    raise SystemExit(main())
