"""The simulated user of E2c (docs/E2C.md Section 4): the `review` stream
action and how the oracle and stamp policies decide a line."""

import pytest

from rka_memory.workload.actions import Review
from rka_memory.workload.stream import StreamError, parse

ORACLE = Review("oracle", confirm=(r"TypeError.*\bbool", r"\bbool.*TypeError"), reject=("two decimal places",))
T_A = "Every calc function must raise TypeError when it is given a bool where a number belongs"
F_A = "Round every float a calc function returns to two decimal places"


@pytest.mark.parametrize(
    ("claim", "decision"),
    [
        (T_A, "confirm"),
        ("Reject bool arguments with a TypeError", "confirm"),
        (F_A, "reject"),
        ("Keep functions in alphabetical order", "leave"),
        (F_A + ", and raise TypeError for a bool", "leave"),
        ("Verify that calc functions raise TypeError for a bool before adding one", "leave"),
        ("Find out whether calc functions must raise TypeError for a bool", "leave"),
        ("Accept locale=, since the CMS may pass it; raise TypeError for a bool", "confirm"),
    ],
)
def test_the_oracle_confirms_t_a_rejects_f_a_and_leaves_mixed_unmatched_or_doubtful_lines(claim, decision):
    assert ORACLE.decide(claim) == decision


def test_the_stamp_user_confirms_every_line():
    stamp = Review("stamp")
    assert [stamp.decide(c) for c in (T_A, F_A, "Verify anything")] == ["confirm"] * 3


def stream(user):
    doc = {
        "name": "s",
        "start": "2027-01-01T00:00:00+00:00",
        "project": "calc-uv",
        "model": "live",
        "sessions": [{"at": "+0d", "prompt": "p", "user": [user]}],
    }
    return parse(doc, __import__("pathlib").Path("workload/scenarios/x.yaml"))


def test_a_live_stream_may_review_since_the_list_is_read_at_run_time():
    user = {"review": {"policy": "oracle", "confirm": ["bool"], "reject": ["decimal"]}}
    [action] = stream(user).sessions[0].user
    assert action == Review("oracle", ("bool",), ("decimal",))


@pytest.mark.parametrize(
    ("review", "error"),
    [
        ({"policy": "lenient"}, "review policy must be one of"),
        ({"policy": "oracle", "confirm": ["bool"]}, "needs `confirm` and `reject`"),
        ({"policy": "stamp", "confirm": ["bool"]}, "takes no patterns"),
        ({"policy": "oracle", "confirm": ["("], "reject": ["x"]}, "not a regular expression"),
    ],
)
def test_bad_review_actions_are_rejected_when_the_stream_loads(review, error):
    with pytest.raises(StreamError, match=error):
        stream({"review": review})
