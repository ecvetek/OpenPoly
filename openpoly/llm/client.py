"""Anthropic API client wrapper for section impls.

A thin **sync** wrapper over ``anthropic.Anthropic``: it resolves the API key
from an ``api_key_ref``, forces a single structured tool call, and returns that
tool's input dict. An optional ``base_url`` routes the call through an
Anthropic-compatible third-party gateway (e.g. yunwu) instead of the
official endpoint. The Anthropic SDK retries transient HTTP errors (429 / 5xx /
connection) itself; this wrapper additionally retries a *structurally* bad
response — one with no ``tool_use`` block, e.g. a refusal — up to
``_STRUCTURAL_RETRIES`` total attempts (one retry after the first) before
raising ``LLMError``.

The call blocks (network I/O); the orchestrator runs the section on a worker
thread so it does not stall the event loop.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from openpoly.news.secrets import SecretsError, resolve

logger = logging.getLogger(__name__)

# The structured tool output is small (an index, a probability, a one-line
# rationale); this caps the response generously.
_MAX_TOKENS = 1024
# Retries when the model returns no tool_use block (the SDK handles HTTP retries).
_STRUCTURAL_RETRIES = 2
# Per-request timeout — a small completion should never take this long.
_TIMEOUT_SECONDS = 60.0

# Model ids observed to reject ``temperature`` with a 400. Learned at runtime
# rather than hardcoded: newer Claude models (Opus 4.7 and later, Sonnet 5,
# Fable 5) removed the sampling parameters entirely, and a hardcoded deny-list
# goes stale on every release — silently, because the symptom is that *every*
# analyzer call errors and the pipeline just stops entering trades. Process-
# lifetime only; a restart re-learns on the first call.
_no_temperature_models: set[str] = set()

# Substring that identifies a sampling-parameter rejection in the API's 400
# message, so we don't retry-without-temperature on an unrelated bad request.
_TEMPERATURE_REJECTED_MARKER = "temperature"

# Minimal tool for the connectivity probe (``ping``): its only job is to make
# the model emit one forced tool_use block.
_PING_TOOL: dict[str, Any] = {
    "name": "ack",
    "description": "Acknowledge the connectivity check.",
    "input_schema": {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    },
}


class LLMError(Exception):
    """An LLM call failed — key resolution, the API, or a malformed response."""


class LLMClient:
    """Sync Anthropic wrapper that runs one forced-tool-call analysis.

    Construction touches nothing; the API key is resolved and the underlying
    ``anthropic.Anthropic`` client is built lazily on the first ``analyze`` —
    so a client can be constructed before the secret store is reachable.
    """

    def __init__(
        self,
        *,
        api_key_ref: str,
        model: str,
        temperature: float,
        base_url: str = "",
    ) -> None:
        self._api_key_ref = api_key_ref
        self._model = model
        self._temperature = temperature
        self._base_url = base_url
        self._client: anthropic.Anthropic | None = None

    def _ensure_client(self) -> anthropic.Anthropic:
        if self._client is None:
            try:
                api_key = resolve(self._api_key_ref)
            except SecretsError as exc:
                raise LLMError(
                    f"could not resolve api_key_ref {self._api_key_ref!r}: {exc}"
                ) from exc
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "max_retries": 2,
                "timeout": _TIMEOUT_SECONDS,
            }
            # Empty base_url → omit it, so the SDK keeps its own default (and
            # still honors an ANTHROPIC_BASE_URL env var); non-empty → route
            # through the given Anthropic-compatible gateway (e.g. yunwu).
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def analyze(self, *, system: str, user: str, tool: dict[str, Any]) -> dict[str, Any]:
        """Run one completion forced to call ``tool``; return its input dict.

        ``tool`` is a full tool definition (``name`` / ``description`` /
        ``input_schema``). Raises ``LLMError`` on key / API failure or when the
        model returns no usable tool call.
        """
        client = self._ensure_client()
        tool_name = str(tool["name"])

        last_error: str | None = None
        for attempt in range(1, _STRUCTURAL_RETRIES + 1):
            try:
                response = self._create(client, system=system, user=user, tool=tool)
            except anthropic.APIError as exc:
                # The SDK already retried transient errors; this is terminal.
                raise LLMError(f"Anthropic API call failed: {exc!r}") from exc

            for block in response.content:
                if block.type == "tool_use" and block.name == tool_name:
                    return dict(block.input)

            last_error = f"no '{tool_name}' tool call (stop_reason={response.stop_reason})"
            logger.warning("LLM attempt %d/%d: %s", attempt, _STRUCTURAL_RETRIES, last_error)

        raise LLMError(f"LLM returned no usable tool call: {last_error}")

    def _create(self, client: anthropic.Anthropic, *, system: str, user: str, tool: dict[str, Any]):
        """One ``messages.create`` call, transparently dropping ``temperature``
        for models that reject it.

        Claude models from Opus 4.7 onward (plus Sonnet 5 and Fable 5) removed
        the sampling parameters, so sending ``temperature`` is a hard 400. This
        used to be a hardcoded deny-list, which meant selecting a newer model in
        the analyzer's canvas config made *every* call fail — the pipeline died
        at the analyzer stage for every news item, with no entry decisions and
        nothing but a wall of errors on the Calls tab.

        So: try with ``temperature``, and if the API rejects it by name, retry
        once without and remember the model for the rest of the process. That
        costs one wasted request per model per process and needs no code change
        when the next model drops sampling params.
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": _MAX_TOKENS,
            # cache_control marks the (static) system prompt cacheable;
            # it is a no-op below the model's minimum cacheable prefix.
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user}],
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": str(tool["name"])},
        }
        if self._model in _no_temperature_models:
            return client.messages.create(**kwargs)
        try:
            return client.messages.create(temperature=self._temperature, **kwargs)
        except anthropic.BadRequestError as exc:
            if _TEMPERATURE_REJECTED_MARKER not in str(exc).lower():
                raise  # a genuine bad request — don't mask it as a sampling issue
            logger.info(
                "model %r rejected temperature; retrying without it (and for "
                "the rest of this process)",
                self._model,
            )
            _no_temperature_models.add(self._model)
            return client.messages.create(**kwargs)

    def ping(self) -> None:
        """Probe connectivity with one minimal forced tool call.

        Reuses ``analyze`` so the probe walks exactly the path a real call
        takes — key resolution, ``base_url`` routing, model-id validity, and
        the forced-tool-call support the analyzer depends on. Raises
        ``LLMError`` on any failure; returns ``None`` on success.
        """
        self.analyze(
            system="Connectivity check.",
            user="Call the ack tool with ok set to true.",
            tool=_PING_TOOL,
        )
