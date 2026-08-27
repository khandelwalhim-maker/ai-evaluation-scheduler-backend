"""GET /api/grid -- had zero test coverage before this file (confirmed: the
only prior hit for "/api/grid" anywhere under tests/ was a curl example in a
docstring, not an assertion). State is seeded directly via
/api/state/restore, matching test_removal.py's convention, rather than going
through a real PDF upload."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

HEADERS = {"X-Session-Id": "test-grid"}


def _seed(client: TestClient) -> None:
    blob = {
        "calendar": {
            "dates": {
                "2026-08-24": {
                    "date": "2026-08-24",
                    "holiday": False,
                    "entries": [
                        {
                            "raw_label": "SDT-SR-10",
                            "row_label": "Division A",
                            "cohort_kind": "division",
                            "cohort_id": "A",
                            "course_guess": "SDT",
                            "course_code": "SDT",
                            "session_numbers": [10],
                            "start": 540,
                            "end": 610,
                            "entry_kind": "class",
                            "confidence": 1.0,
                        }
                    ],
                }
            },
            "cohorts": {"divisions": ["A"], "minors": []},
        },
    }
    resp = client.post("/api/state/restore", json=blob, headers=HEADERS)
    assert resp.status_code == 200


def test_grid_includes_cohort_and_course_code_fields():
    client = TestClient(app)
    _seed(client)

    resp = client.get("/api/grid?week_start=2026-08-24", headers=HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    day = next(d for d in body["days"] if d["date"] == "2026-08-24")
    assert len(day["classes"]) == 1

    entry = day["classes"][0]
    assert entry["cohort_kind"] == "division"
    assert entry["cohort_id"] == "A"
    assert entry["course_code"] == "SDT"
    assert entry["course"] == "SDT"
    assert entry["start_time"] == "09:00"
    assert entry["end_time"] == "10:10"


def test_grid_empty_week_returns_empty_days():
    client = TestClient(app)
    _seed(client)

    resp = client.get("/api/grid?week_start=2026-09-01", headers=HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    assert len(body["days"]) == 7
    assert all(d["classes"] == [] and d["assessments"] == [] for d in body["days"])


def test_grid_rejects_bad_week_start():
    client = TestClient(app)
    _seed(client)

    resp = client.get("/api/grid?week_start=not-a-date", headers=HEADERS)
    assert resp.status_code == 400
