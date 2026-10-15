import pytest

from scripts.verify_realtime_load import RealtimeVerification, is_accept_event


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com",
        "http://192.168.1.5",
        "http://127.0.0.1@remote.example",
        "http://localhost/path",
    ],
)
def test_realtime_verification_rejects_remote_or_ambiguous_origins(target):
    with pytest.raises(ValueError):
        RealtimeVerification(target)


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"payload": None},
        {"payload": "invalid"},
        {"payload": {"id": "different", "action": "accept"}},
        {"payload": {"id": "order", "action": "start"}},
    ],
)
def test_only_target_accept_event_counts_toward_latency(message):
    assert not is_accept_event(message, "order")


def test_target_accept_event_counts_toward_latency():
    assert is_accept_event({"payload": {"id": "order", "action": "accept"}}, "order")
