"""DELETE /api/course/{index} and DELETE /api/timetable -- undoing a wrong
upload from the frontend. No LLM calls; state is seeded directly via
/api/state/restore rather than going through /api/upload."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

HEADERS = {"X-Session-Id": "test-removal"}


def _seed(client: TestClient) -> None:
    blob = {
        "calendar": {
            "dates": {
                "2026-08-24": {
                    "date": "2026-08-24",
                    "holiday": False,
                    "entries": [
                        {
                            "raw_label": "ABA-1",
                            "row_label": "Core",
                            "cohort_kind": "division",
                            "cohort_id": "A",
                            "entry_kind": "class",
                        }
                    ],
                }
            },
            "cohorts": {"divisions": ["A"], "minors": []},
            "courses": [{"name": "Applied Business Analytics", "code": "ABA"}, {"name": "Marketing"}],
        },
        "confirmation_queue": [{"kind": "identity", "question": "Is EAB the same as ABA?", "context": "EAB"}],
    }
    resp = client.post("/api/state/restore", json=blob, headers=HEADERS)
    assert resp.status_code == 200


def test_remove_course_by_index():
    client = TestClient(app)
    _seed(client)

    resp = client.delete("/api/course/0", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["removed"]["name"] == "Applied Business Analytics"

    state = client.get("/api/state", headers=HEADERS).json()
    names = [c["name"] for c in state["calendar"]["courses"]]
    assert names == ["Marketing"]


def test_remove_course_out_of_range_404s():
    client = TestClient(app)
    _seed(client)

    resp = client.delete("/api/course/99", headers=HEADERS)
    assert resp.status_code == 404

    state = client.get("/api/state", headers=HEADERS).json()
    assert len(state["calendar"]["courses"]) == 2


def test_clear_timetable_drops_dates_cohorts_and_questions():
    client = TestClient(app)
    _seed(client)

    resp = client.delete("/api/timetable", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cleared"

    state = client.get("/api/state", headers=HEADERS).json()
    assert state["calendar"]["dates"] == {}
    assert state["calendar"]["cohorts"] == {"divisions": [], "minors": []}
    assert state["confirmation_queue"] == []
    # Course outlines are untouched by clearing the timetable.
    assert len(state["calendar"]["courses"]) == 2
