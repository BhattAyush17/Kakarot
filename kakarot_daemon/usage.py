"""Per-turn request and token accounting for Gemini calls.

One user request is not one API request.  The daemon may call the model once for
the answer, once per tool round, and once more for text-to-speech; retries and
fallback models can add further attempts.  This module keeps that cost visible
without pulling billing data from Google.

The SDK uses ``candidates_token_count`` for ``generateContent`` responses and
``response_token_count`` for Live responses.  Both names are accepted, and a
missing or partial ``usage_metadata`` must never break a turn.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any

_USAGE_FIELDS = {
    "prompt_token_count",
    "candidates_token_count",
    "response_token_count",
    "thoughts_token_count",
    "tool_use_prompt_token_count",
    "cached_content_token_count",
    "total_token_count",
}


def _raw(usage: Any, name: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(name)
    return getattr(usage, name, None)


def _count(usage: Any, *names: str) -> int:
    """First present, non-negative integer among ``names``."""
    for name in names:
        value = _raw(usage, name)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0
    return 0


@dataclass
class UsageTotals:
    """Aggregated Gemini usage for one turn or for the whole process."""

    turns: int = 0
    requests: int = 0
    successful_requests: int = 0
    prompt_tokens: int = 0
    candidate_tokens: int = 0
    thoughts_tokens: int = 0
    tool_use_prompt_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    tts_requests: int = 0
    tts_successful_requests: int = 0
    tts_prompt_tokens: int = 0
    tts_candidate_tokens: int = 0
    tts_total_tokens: int = 0

    # -- recording --------------------------------------------------------

    def begin_request(self, *, tts: bool = False) -> None:
        """Count an attempt, including one that fails before returning usage."""
        if tts:
            self.tts_requests += 1
        else:
            self.requests += 1

    def add_usage(self, usage: Any, *, tts: bool = False) -> None:
        """Add token counts from one SDK usage object or mapping."""
        if usage is None:
            return
        prompt = _count(usage, "prompt_token_count")
        candidate = _count(
            usage, "candidates_token_count", "response_token_count"
        )
        thoughts = _count(usage, "thoughts_token_count")
        tool = _count(usage, "tool_use_prompt_token_count")
        cached = _count(usage, "cached_content_token_count")
        total = _count(usage, "total_token_count")
        if total <= 0:
            # Older or partial SDK responses omit the total; account the
            # components rather than silently reporting zero.
            total = prompt + candidate + thoughts + tool
        if tts:
            self.tts_prompt_tokens += prompt
            self.tts_candidate_tokens += candidate
            self.tts_total_tokens += total
            return
        self.prompt_tokens += prompt
        self.candidate_tokens += candidate
        self.thoughts_tokens += thoughts
        self.tool_use_prompt_tokens += tool
        self.cached_tokens += cached
        self.total_tokens += total

    def record_response(self, usage: Any, *, tts: bool = False) -> None:
        """Record one successful API response, with or without usage data."""
        if tts:
            self.tts_successful_requests += 1
        else:
            self.successful_requests += 1
        self.add_usage(usage, tts=tts)

    # -- aggregation ------------------------------------------------------

    def merge(self, other: "UsageTotals") -> None:
        """Add another ledger (normally a turn) into this one."""
        for field in fields(other):
            setattr(
                self,
                field.name,
                getattr(self, field.name) + int(getattr(other, field.name)),
            )

    def copy(self) -> "UsageTotals":
        return UsageTotals(
            **{field.name: getattr(self, field.name) for field in fields(self)}
        )

    def as_dict(self) -> dict[str, int]:
        data = asdict(self)
        # ``requests`` counts model calls; TTS is a separate API request. Make
        # the combined figure explicit for status clients and audit entries.
        data["total_requests"] = self.requests + self.tts_requests
        return data

    def details(self) -> str:
        """Compact, machine-readable form for the audit trail."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    def summary(self) -> str:
        return (
            f"turns={self.turns} "
            f"total_requests={self.requests + self.tts_requests} "
            f"requests={self.requests} "
            f"(ok={self.successful_requests}) "
            f"prompt={self.prompt_tokens} output={self.candidate_tokens} "
            f"thoughts={self.thoughts_tokens} tool={self.tool_use_prompt_tokens} "
            f"cached={self.cached_tokens} total={self.total_tokens} "
            f"tts_requests={self.tts_requests} "
            f"(ok={self.tts_successful_requests}) "
            f"tts_total={self.tts_total_tokens}"
        )


def usage_field_names() -> frozenset[str]:
    """The SDK fields this module reads; exported for diagnostics/tests."""
    return frozenset(_USAGE_FIELDS)
