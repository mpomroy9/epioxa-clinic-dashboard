import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "outputs"))

import build_epioxa_dashboard as dashboard  # pyright: ignore[reportMissingImports]  # noqa: E402
import epioxa_weekly_monitor as monitor  # pyright: ignore[reportMissingImports]  # noqa: E402


class MonitorValidationTests(unittest.TestCase):
    def test_accepts_complete_stable_pull(self):
        monitor.validate_collection(
            current_count=439,
            query_count=112,
            expected_query_count=112,
            previous_count=438,
        )

    def test_rejects_partial_query_coverage(self):
        with self.assertRaisesRegex(RuntimeError, "111 of 112"):
            monitor.validate_collection(
                current_count=439,
                query_count=111,
                expected_query_count=112,
                previous_count=438,
            )

    def test_ratio_override_does_not_allow_partial_queries(self):
        with self.assertRaisesRegex(RuntimeError, "111 of 112"):
            monitor.validate_collection(
                current_count=300,
                query_count=111,
                expected_query_count=112,
                previous_count=439,
                minimum_live_ratio=0,
                maximum_live_ratio=float("inf"),
            )

    def test_rejects_large_live_count_drop(self):
        with self.assertRaisesRegex(RuntimeError, "below minimum"):
            monitor.validate_collection(
                current_count=300,
                query_count=112,
                expected_query_count=112,
                previous_count=439,
            )

    def test_rejects_large_live_count_growth(self):
        with self.assertRaisesRegex(RuntimeError, "exceeds maximum"):
            monitor.validate_collection(
                current_count=600,
                query_count=112,
                expected_query_count=112,
                previous_count=439,
            )

    def test_rejects_unresolved_api_saturation(self):
        with self.assertRaisesRegex(RuntimeError, "API ceiling"):
            monitor.validate_collection(
                current_count=542,
                query_count=300,
                expected_query_count=300,
                previous_count=545,
                unresolved_saturation=["coverage New York / detection"],
            )

    def test_baseline_run_is_derived_from_status(self):
        conn = sqlite3.connect(":memory:")
        monitor.init_db(conn)
        conn.execute(
            """
            INSERT INTO runs (
                id, run_at, source, total_facilities, new_facilities, status, notes
            ) VALUES (7, '2026-07-01T00:00:00+00:00', 'test', 398, 0, 'baseline', '')
            """
        )
        conn.commit()
        self.assertEqual(monitor.get_baseline_run_id(conn), 7)

    def test_refresh_target_places_uses_stored_coordinates(self):
        conn = sqlite3.connect(":memory:")
        monitor.init_db(conn)
        facility = {
            "id": "facility-1",
            "name": "Test Eye Care",
            "address": {"city": "Testville", "state": "TN", "zipCode": "37135"},
            "coordinates": {"latitude": 35.9, "longitude": -86.6},
            "facilityType": {"isDetectionCenter": True},
        }
        monitor.upsert_facility(conn, facility, 1, "2026-09-16T00:00:00+00:00")
        conn.commit()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.json"
            with patch.object(
                monitor,
                "query_place_id_from_coordinates",
                return_value={"zipCode": "37135", "placeId": "place-1"},
            ):
                added, skipped, targets = monitor.refresh_target_places(conn, path)
            self.assertEqual((added, skipped), (1, 0))
            self.assertEqual(targets["facility-1"]["placeId"], "place-1")
            self.assertEqual(json.loads(path.read_text())["byFacilityId"]["facility-1"]["source"], "Epioxa coords-to-zip")

    def test_supplemental_seeds_are_spatially_distributed(self):
        conn = sqlite3.connect(":memory:")
        monitor.init_db(conn)
        targets = {}
        for facility_id, latitude, longitude in (
            ("near-1", 35.90, -86.60),
            ("near-2", 35.95, -86.65),
            ("far", 40.71, -74.00),
        ):
            facility = {
                "id": facility_id,
                "name": facility_id,
                "address": {"city": "Test", "state": "TN", "zipCode": "00000"},
                "coordinates": {"latitude": latitude, "longitude": longitude},
                "facilityType": {"isDetectionCenter": True},
            }
            monitor.upsert_facility(conn, facility, 1, "2026-09-16T00:00:00+00:00")
            targets[facility_id] = {"placeId": f"place-{facility_id}"}
        seeds = monitor.select_supplemental_places(conn, targets, spacing_miles=50)
        self.assertEqual(len(seeds), 2)

    def test_capped_broad_search_adds_supplemental_queries(self):
        conn = sqlite3.connect(":memory:")
        monitor.init_db(conn)
        broad_facilities = [
            {"id": f"broad-{index}", "name": f"Broad {index}"}
            for index in range(monitor.API_RESULT_LIMIT)
        ]
        supplemental_facility = {"id": "supplemental", "name": "Supplemental"}
        supplemental_place = {
            "query": "coverage Test, TN 00000",
            "text": "00000",
            "placeId": "supplemental-place",
            "coordinates": (35.9, -86.6),
        }
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "baseline.json"
            targets = Path(directory) / "targets.json"
            baseline.write_text(
                json.dumps(
                    {
                        "placeRows": [
                            {"query": "Test, TN", "text": "Test, TN", "placeId": "base-place"}
                        ]
                    }
                )
            )
            with (
                patch.object(
                    monitor,
                    "query_epioxa",
                    return_value={"facilities": broad_facilities},
                ),
                patch.object(
                    monitor,
                    "query_epioxa_by_place_id",
                    return_value={"facilities": [supplemental_facility]},
                ),
                patch.object(
                    monitor,
                    "select_supplemental_places",
                    return_value=[supplemental_place],
                ),
                patch.object(
                    monitor,
                    "targeted_recheck_missing",
                    return_value=([], []),
                ),
            ):
                current, _, query_results, expected, meta = monitor.collect_current_facilities(
                    conn, baseline, 0, target_places_path=targets
                )
        self.assertEqual(len(query_results), expected)
        self.assertEqual(expected, 4)
        self.assertEqual(meta["supplemental_query_count"], 2)
        self.assertIn("supplemental", current)


class DashboardTests(unittest.TestCase):
    def test_week_start_is_monday_in_utc(self):
        self.assertEqual(dashboard.week_start("2026-07-25T13:10:49+00:00"), "2026-07-20")

    def test_embedded_json_cannot_close_script_element(self):
        rendered = dashboard.render_dashboard(
            {
                "generated_at": "2026-07-25T13:10:49+00:00",
                "facilities": [{"name": "</script><script>alert(1)</script>"}],
            }
        )
        self.assertNotIn("</script><script>alert(1)</script>", rendered)
        self.assertIn("\\u003c/script\\u003e", rendered)


if __name__ == "__main__":
    unittest.main()
