"""Context-length estimation from transcript usage blocks."""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from toolcheck.transcript import (
    context_length_from_entries,
    context_length_from_lines,
    usage_total,
)


def _assistant(usage: dict[str, object]) -> dict[str, object]:
    return {"type": "assistant", "message": {"role": "assistant", "usage": usage}}


def test_usage_total_sums_the_four_token_keys():
    usage = {
        "input_tokens": 8,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 30000,
        "output_tokens": 200,
        "service_tier": "standard",  # ignored
    }
    assert usage_total(usage) == 31208


def test_usage_total_none_when_no_token_keys():
    assert usage_total({"service_tier": "standard"}) is None


def test_last_usage_bearing_entry_wins():
    entries = [
        _assistant({"input_tokens": 1, "cache_read_input_tokens": 10}),
        {"type": "user", "message": {"role": "user"}},  # no usage
        _assistant({"input_tokens": 2, "cache_read_input_tokens": 5000}),
    ]
    assert context_length_from_entries(entries) == 5002


def test_none_when_no_usage_anywhere():
    entries = [{"type": "user", "message": {"role": "user"}}, {"type": "summary"}]
    assert context_length_from_entries(entries) is None


def test_empty_transcript_is_unknown():
    assert context_length_from_entries([]) is None


def test_lines_skips_malformed_json():
    good = _assistant({"input_tokens": 3, "cache_read_input_tokens": 100})
    lines = ["", "not json", json.dumps(good), "  "]
    assert context_length_from_lines(lines) == 103


@given(
    st.integers(min_value=0, max_value=10**6),
    st.integers(min_value=0, max_value=10**6),
    st.integers(min_value=0, max_value=10**6),
    st.integers(min_value=0, max_value=10**6),
)
def test_usage_total_is_the_sum(i: int, cc: int, cr: int, o: int):
    usage = {
        "input_tokens": i,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
        "output_tokens": o,
    }
    assert usage_total(usage) == i + cc + cr + o
