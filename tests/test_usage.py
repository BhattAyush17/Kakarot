"""Accounting tests for per-turn Gemini request/token usage."""

from __future__ import annotations

from types import SimpleNamespace

from kakarot_daemon.state import DaemonState
from kakarot_daemon.usage import UsageTotals


def test_rest_usage_counts_requests_and_token_fields() -> None:
    usage = UsageTotals(turns=1)
    usage.begin_request()
    usage.record_response(
        SimpleNamespace(
            prompt_token_count=120,
            candidates_token_count=30,
            thoughts_token_count=7,
            tool_use_prompt_token_count=11,
            cached_content_token_count=40,
            total_token_count=168,
        )
    )

    assert usage.requests == 1
    assert usage.successful_requests == 1
    assert usage.prompt_tokens == 120
    assert usage.candidate_tokens == 30
    assert usage.thoughts_tokens == 7
    assert usage.tool_use_prompt_tokens == 11
    assert usage.cached_tokens == 40
    assert usage.total_tokens == 168
    assert usage.as_dict()["total_requests"] == 1


def test_live_usage_accepts_response_token_count() -> None:
    usage = UsageTotals(turns=1)
    usage.begin_request()
    usage.add_usage(
        SimpleNamespace(
            prompt_token_count=90,
            response_token_count=12,
            total_token_count=102,
        )
    )
    usage.successful_requests = 1

    assert usage.candidate_tokens == 12
    assert usage.total_tokens == 102


def test_missing_usage_still_records_a_successful_request() -> None:
    usage = UsageTotals(turns=1)
    usage.begin_request()
    usage.record_response(None)

    assert usage.requests == 1
    assert usage.successful_requests == 1
    assert usage.total_tokens == 0


def test_total_is_derived_when_the_sdk_omits_it() -> None:
    usage = UsageTotals(turns=1)
    usage.record_response(
        {
            "prompt_token_count": 50,
            "candidates_token_count": 5,
            "thoughts_token_count": 2,
            "tool_use_prompt_token_count": 3,
        }
    )

    assert usage.total_tokens == 60


def test_tts_usage_is_kept_separate_from_answer_usage() -> None:
    usage = UsageTotals(turns=1)
    usage.begin_request()
    usage.record_response({"prompt_token_count": 10, "total_token_count": 12})
    usage.begin_request(tts=True)
    usage.record_response(
        {"prompt_token_count": 20, "candidates_token_count": 4, "total_token_count": 24},
        tts=True,
    )

    assert usage.requests == 1
    assert usage.total_tokens == 12
    assert usage.tts_requests == 1
    assert usage.tts_successful_requests == 1
    assert usage.tts_total_tokens == 24
    assert usage.as_dict()["total_requests"] == 2


def test_session_accumulator_merges_whole_turns() -> None:
    session = UsageTotals()
    first = UsageTotals(turns=1)
    first.begin_request()
    first.record_response({"prompt_token_count": 10, "total_token_count": 10})
    second = UsageTotals(turns=1)
    second.begin_request()
    second.record_response({"prompt_token_count": 20, "total_token_count": 20})

    session.merge(first)
    session.merge(second)

    assert session.turns == 2
    assert session.requests == 2
    assert session.prompt_tokens == 30
    assert session.total_tokens == 30


def test_daemon_state_publishes_a_copy_of_the_usage_ledger() -> None:
    state = DaemonState()
    ledger = {"requests": 2, "total_tokens": 42}
    state.apply(last_turn_usage=ledger, session_usage=ledger)

    ledger["requests"] = 99
    snapshot = state.snapshot()

    assert snapshot["last_turn_usage"]["requests"] == 2
    assert snapshot["session_usage"]["total_tokens"] == 42
