from __future__ import annotations

import json
import time
from typing import Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app import config

T = TypeVar("T", bound=BaseModel)

RATE_LIMIT_ATTEMPTS = 3
REQUEST_TIMEOUT = 60.0
# Groq's current models spend part of this budget on hidden reasoning tokens
# before the visible answer; too low a value yields an empty completion.
DEFAULT_MAX_TOKENS = 8000
# Groq's 429 retry-after can reflect a daily quota reset (minutes to hours
# away); sleeping for that long inside a live request handler hung an
# upload for over 80 minutes before it was killed manually (see
# docs/HANDOFF_V2.md, known issue #2). Past this cap we fail fast instead --
# the caller gets a prompt 502 and can retry once the quota resets, rather
# than the process blocking a worker for the full wait.
MAX_RETRY_DELAY = 5.0


class LLMError(RuntimeError):
    pass


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return min(float(retry_after), MAX_RETRY_DELAY)
        except ValueError:
            pass
    return min(float(2**attempt), MAX_RETRY_DELAY)


def _is_rate_limited(response: httpx.Response) -> bool:
    # Groq signals "not enough tokens left in this window" either as a plain
    # 429, or as a 413 whose body still self-identifies as a token rate limit
    # (as opposed to a genuinely oversized, unretryable request).
    if response.status_code == 429:
        return True
    if response.status_code == 413:
        try:
            error = response.json().get("error", {})
        except ValueError:
            return False
        return error.get("code") == "rate_limit_exceeded"
    return False


class LLMClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        resolved_key = api_key or config.GROQ_API_KEY
        if not resolved_key:
            raise LLMError("GROQ_API_KEY is not configured; cannot create an LLMClient")
        self._api_key = resolved_key
        self._base_url = (base_url or config.LLM_BASE_URL).rstrip("/")

    def _chat(
        self,
        system_prompt: str,
        user_content: str,
        model: str,
        *,
        json_mode: bool,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self._api_key}"}
        url = f"{self._base_url}/chat/completions"

        last_exc: Exception | None = None
        for attempt in range(RATE_LIMIT_ATTEMPTS):
            response = httpx.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            if _is_rate_limited(response):
                last_exc = LLMError(f"rate limited (attempt {attempt + 1}): {response.text}")
                if attempt == RATE_LIMIT_ATTEMPTS - 1:
                    break
                time.sleep(_retry_delay(response, attempt))
                continue
            if response.status_code >= 400:
                raise LLMError(f"LLM request failed with {response.status_code}: {response.text}")
            data = response.json()
            return data["choices"][0]["message"]["content"] or ""

        raise LLMError(f"rate limited after {RATE_LIMIT_ATTEMPTS} attempts") from last_exc

    def complete_text(
        self, system_prompt: str, user_content: str, model: str, *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> str:
        try:
            return self._chat(system_prompt, user_content, model, json_mode=False, max_tokens=max_tokens)
        except LLMError:
            if model == config.MODEL_FALLBACK:
                raise
            return self._chat(
                system_prompt, user_content, config.MODEL_FALLBACK, json_mode=False, max_tokens=max_tokens
            )

    def complete_json(
        self,
        system_prompt: str,
        user_content: str,
        schema_model: Type[T],
        model: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> T:
        return self._complete_json_with_model(
            system_prompt, user_content, schema_model, model, max_tokens
        )

    def _complete_json_with_model(
        self,
        system_prompt: str,
        user_content: str,
        schema_model: Type[T],
        model: str,
        max_tokens: int,
    ) -> T:
        schema_hint = (
            f"{user_content}\n\nRespond with a single JSON object matching this JSON schema:\n"
            f"{json.dumps(schema_model.model_json_schema())}"
        )

        try:
            raw = self._chat(system_prompt, schema_hint, model, json_mode=True, max_tokens=max_tokens)
        except LLMError:
            # Retry the same model once before escalating -- Groq occasionally
            # 400s with an empty completion under json_mode (json_validate_failed)
            # on an otherwise-fine request, and simply asking again often
            # succeeds. Mirrors the same-model retry already done below for a
            # response that came back but failed schema validation.
            try:
                raw = self._chat(system_prompt, schema_hint, model, json_mode=True, max_tokens=max_tokens)
            except LLMError:
                if model == config.MODEL_FALLBACK:
                    raise
                return self._complete_json_with_model(
                    system_prompt, user_content, schema_model, config.MODEL_FALLBACK, max_tokens
                )

        try:
            return schema_model.model_validate_json(raw)
        except (ValidationError, json.JSONDecodeError) as exc:
            retry_content = (
                f"{schema_hint}\n\nYour previous response failed validation with this error:\n"
                f"{exc}\n\nPrevious response:\n{raw}\n\nReturn a corrected JSON object only, "
                f"with no commentary."
            )
            try:
                raw_retry = self._chat(
                    system_prompt, retry_content, model, json_mode=True, max_tokens=max_tokens
                )
                return schema_model.model_validate_json(raw_retry)
            except (ValidationError, json.JSONDecodeError) as exc2:
                if model == config.MODEL_FALLBACK:
                    raise LLMError(f"schema validation failed twice: {exc2}") from exc2
                return self._complete_json_with_model(
                    system_prompt, user_content, schema_model, config.MODEL_FALLBACK, max_tokens
                )
            except LLMError:
                if model == config.MODEL_FALLBACK:
                    raise
                return self._complete_json_with_model(
                    system_prompt, user_content, schema_model, config.MODEL_FALLBACK, max_tokens
                )
