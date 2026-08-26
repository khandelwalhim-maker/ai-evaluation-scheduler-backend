import httpx
from fastapi.testclient import TestClient

from app import config
from app.llm import LLMClient, LLMError
from app.main import app

client = TestClient(app)

ADMIN_HEADER = "X-Admin-Token"


def _bootstrap_session(session_id: str) -> None:
    # require_session 404s for a session_id the process has never seen
    # (see main.py's require_session docstring) -- /state/restore is the
    # one route that creates/replaces a session without requiring it to
    # already exist, same as the frontend's 404-recovery path in api.ts.
    response = client.post("/api/state/restore", json={}, headers={"X-Session-Id": session_id})
    assert response.status_code == 200


def test_admin_settings_fails_closed_when_token_not_configured(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", None)
    response = client.get("/api/admin/settings", headers={ADMIN_HEADER: "whatever"})
    assert response.status_code == 503


def test_admin_settings_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "correct-token")
    response = client.get("/api/admin/settings", headers={ADMIN_HEADER: "wrong-token"})
    assert response.status_code == 401


def test_admin_settings_accepts_correct_token_and_masks_key(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "correct-token")
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-super-secret-key-1234")

    response = client.get("/api/admin/settings", headers={ADMIN_HEADER: "correct-token"})

    assert response.status_code == 200
    body = response.json()
    assert body["llm_api_key_masked"].endswith("1234")
    assert "super-secret" not in body["llm_api_key_masked"]


def test_admin_settings_update_applies_immediately_via_live_config_access(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "correct-token")
    monkeypatch.setattr(config, "MODEL_PARSE", "old-model")

    response = client.post(
        "/api/admin/settings",
        json={"model_parse": "new-model"},
        headers={ADMIN_HEADER: "correct-token"},
    )

    assert response.status_code == 200
    assert response.json()["changed"] == ["model_parse"]
    assert config.MODEL_PARSE == "new-model", "config.MODEL_PARSE should be mutated in place"


def test_admin_settings_update_blank_model_is_a_no_op_not_a_clear(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "correct-token")
    monkeypatch.setattr(config, "MODEL_PARSE", "keep-me")

    response = client.post(
        "/api/admin/settings",
        json={"model_parse": ""},
        headers={ADMIN_HEADER: "correct-token"},
    )

    assert response.status_code == 200
    assert response.json()["changed"] == []
    assert config.MODEL_PARSE == "keep-me"


def test_admin_settings_update_blank_extra_instructions_does_clear(monkeypatch):
    from app import admin_settings

    monkeypatch.setattr(config, "ADMIN_TOKEN", "correct-token")
    monkeypatch.setitem(admin_settings.EXTRA_INSTRUCTIONS, "intent", "some previous instruction")

    response = client.post(
        "/api/admin/settings",
        json={"extra_intent_instructions": ""},
        headers={ADMIN_HEADER: "correct-token"},
    )

    assert response.status_code == 200
    assert response.json()["changed"] == ["extra_intent_instructions"]
    assert admin_settings.EXTRA_INSTRUCTIONS["intent"] == ""


def test_rotate_admin_token_requires_current_token(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "correct-token")
    response = client.post(
        "/api/admin/token",
        json={"new_token": "a-brand-new-token"},
        headers={ADMIN_HEADER: "wrong-token"},
    )
    assert response.status_code == 401
    assert config.ADMIN_TOKEN == "correct-token", "a failed rotation must not change anything"


def test_rotate_admin_token_rejects_too_short(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "correct-token")
    response = client.post(
        "/api/admin/token",
        json={"new_token": "short"},
        headers={ADMIN_HEADER: "correct-token"},
    )
    assert response.status_code == 400
    assert config.ADMIN_TOKEN == "correct-token"


def test_rotate_admin_token_takes_effect_immediately(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "old-correct-token")

    response = client.post(
        "/api/admin/token",
        json={"new_token": "brand-new-longer-token"},
        headers={ADMIN_HEADER: "old-correct-token"},
    )
    assert response.status_code == 200
    assert config.ADMIN_TOKEN == "brand-new-longer-token"

    # The old token must stop working immediately, and the new one must work.
    old_still_works = client.get("/api/admin/settings", headers={ADMIN_HEADER: "old-correct-token"})
    assert old_still_works.status_code == 401

    new_works = client.get("/api/admin/settings", headers={ADMIN_HEADER: "brand-new-longer-token"})
    assert new_works.status_code == 200


def test_admin_settings_test_classifies_auth_error_without_raw_text(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "correct-token")
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-super-secret-key-1234")
    secret_marker = "sk-super-secret-key-1234-in-provider-response"

    def fake_chat(self, *args, **kwargs):
        raise LLMError(f"LLM request failed with 401: {secret_marker}")

    monkeypatch.setattr(LLMClient, "_chat", fake_chat)

    response = client.post("/api/admin/settings/test", headers={ADMIN_HEADER: "correct-token"})

    assert response.status_code == 200
    assert response.json()["status"] == "auth_error"
    assert secret_marker not in response.text


def test_admin_settings_test_classifies_network_failure_as_other_error(monkeypatch):
    # The defensive fallback, not just the happy path: _chat()'s httpx.post
    # isn't wrapped in its own try/except, so a network failure surfaces as
    # a raw httpx exception, not an LLMError -- this must not become a 500.
    monkeypatch.setattr(config, "ADMIN_TOKEN", "correct-token")
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-super-secret-key-1234")

    def fake_chat(self, *args, **kwargs):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(LLMClient, "_chat", fake_chat)

    response = client.post("/api/admin/settings/test", headers={ADMIN_HEADER: "correct-token"})

    assert response.status_code == 200, "a network failure must not surface as a 500"
    assert response.json()["status"] == "other_error"


def test_upload_chat_selfcheck_never_forward_raw_upstream_text(monkeypatch):
    # Regression guard for the leak mechanism found while planning this
    # feature: llm.py's LLMError can carry the full raw upstream response
    # body (see _chat()'s raise site). These three pre-existing routes must
    # never put that text in a client-visible response body.
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-super-secret-key-1234")
    secret_marker = "upstream-secret-marker-xyz"

    def fake_chat(self, *args, **kwargs):
        raise LLMError(f"LLM request failed with 401: {secret_marker}")

    monkeypatch.setattr(LLMClient, "_chat", fake_chat)

    selfcheck = client.get("/api/selfcheck")
    assert secret_marker not in selfcheck.text

    session_id = "test-admin-leak-guard"
    _bootstrap_session(session_id)
    chat = client.post("/api/chat", json={"message": "hello"}, headers={"X-Session-Id": session_id})
    assert secret_marker not in chat.text
