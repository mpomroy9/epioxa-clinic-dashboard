import sqlite3
import sys
import unittest
from pathlib import Path


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
