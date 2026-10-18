from unittest import mock

import googleapiclient.errors
import httplib2
import pytest

import lieer.remote
from lieer.remote import Remote


def http_error(status):
    return googleapiclient.errors.HttpError(httplib2.Response({"status": status}), b"")


@pytest.fixture
def remote(monkeypatch):
    """
    A Remote with a mocked Gmail service (no local repository needed).
    """
    monkeypatch.setattr(lieer.remote.time, "sleep", lambda s: None)

    r = Remote.__new__(Remote)
    r.authorized = True
    r.account = "me"
    r._delay = 0
    r._delay_ok = 0
    r.service = mock.MagicMock()
    return r


def changes(n, add=("A",), rem=("INBOX",), start=0):
    return [Remote.Change(f"id{i}", add, rem) for i in range(start, start + n)]


def batch_modify_calls(remote):
    return remote.service.users().messages().batchModify.call_args_list


def test_groups_with_the_same_change_use_one_request(remote):
    done = []
    individually = mock.Mock()
    remote.__push_individually__ = individually

    actions = changes(12) + changes(12, add=("B",), start=100)
    remote.push_changes(actions, done.append)

    calls = batch_modify_calls(remote)
    assert len(calls) == 2
    assert calls[0].kwargs["body"]["ids"] == [f"id{i}" for i in range(12)]
    assert calls[0].kwargs["body"]["addLabelIds"] == ["A"]
    assert calls[0].kwargs["body"]["removeLabelIds"] == ["INBOX"]
    assert calls[1].kwargs["body"]["addLabelIds"] == ["B"]
    assert len(done) == 24
    individually.assert_not_called()


def test_large_groups_are_chunked(remote):
    n = Remote.BATCH_MODIFY_SIZE * 2 + 500
    done = []
    remote.push_changes(changes(n), done.append)

    sizes = [len(c.kwargs["body"]["ids"]) for c in batch_modify_calls(remote)]
    assert sizes == [Remote.BATCH_MODIFY_SIZE, Remote.BATCH_MODIFY_SIZE, 500]
    assert len(done) == n


def test_small_groups_use_individual_requests(remote):
    remote.__push_individually__ = mock.Mock()

    n = Remote.MIN_BATCH_MODIFY_GROUP - 1
    remote.push_changes(changes(n), lambda _: None)

    assert batch_modify_calls(remote) == []
    (reqs, _), _ = remote.__push_individually__.call_args
    assert len(reqs) == n


def test_rate_limit_is_retried(remote):
    execute = remote.service.users().messages().batchModify().execute
    execute.side_effect = [http_error(429), http_error(503), None]
    remote.service.users().messages().batchModify.reset_mock()

    done = []
    remote.push_changes(changes(20), done.append)

    assert execute.call_count == 3
    assert len(done) == 20


def test_rejected_request_falls_back_to_individual_requests(remote):
    execute = remote.service.users().messages().batchModify().execute
    execute.side_effect = http_error(404)
    remote.__push_individually__ = mock.Mock()

    done = []
    remote.push_changes(changes(15), done.append)

    # the changes are handed to the per-message path, which reports progress
    assert done == []
    (reqs, _), _ = remote.__push_individually__.call_args
    assert len(reqs) == 15


def test_other_errors_are_raised(remote):
    execute = remote.service.users().messages().batchModify().execute
    execute.side_effect = http_error(401)

    with pytest.raises(googleapiclient.errors.HttpError):
        remote.push_changes(changes(20), lambda _: None)


def test_same_add_different_rem_are_separate_groups(remote):
    remote.__push_individually__ = mock.Mock()

    remote.push_changes(
        changes(12) + changes(12, rem=("UNREAD",), start=100), lambda _: None
    )

    bodies = sorted(
        (c.kwargs["body"]["removeLabelIds"], len(c.kwargs["body"]["ids"]))
        for c in batch_modify_calls(remote)
    )
    assert bodies == [(["INBOX"], 12), (["UNREAD"], 12)]
    remote.__push_individually__.assert_not_called()


def test_group_of_exactly_min_size_is_batched(remote):
    remote.__push_individually__ = mock.Mock()

    remote.push_changes(changes(Remote.MIN_BATCH_MODIFY_GROUP), lambda _: None)

    assert len(batch_modify_calls(remote)) == 1
    remote.__push_individually__.assert_not_called()


def test_mixed_push_batches_big_group_and_falls_back_small(remote):
    remote.__push_individually__ = mock.Mock()

    done = []
    remote.push_changes(changes(12) + changes(3, add=("B",), start=100), done.append)

    calls = batch_modify_calls(remote)
    assert len(calls) == 1
    assert calls[0].kwargs["body"]["addLabelIds"] == ["A"]
    assert len(done) == 12
    (reqs, cb), _ = remote.__push_individually__.call_args
    assert len(reqs) == 3
    assert cb == done.append


def test_fallback_requests_keep_labels(remote):
    remote.__push_individually__ = mock.Mock()

    remote.push_changes([Remote.Change("x", ("A", "B"), ("INBOX",))], lambda _: None)

    remote.service.users().messages().modify.assert_called_once_with(
        userId="me",
        id="x",
        body={"addLabelIds": ["A", "B"], "removeLabelIds": ["INBOX"]},
    )


def test_persistent_rate_limit_gives_up(remote):
    # a finite list: if the back-off stops working this fails with
    # StopIteration instead of looping forever
    execute = remote.service.users().messages().batchModify().execute
    execute.side_effect = [http_error(429)] * 50

    with pytest.raises(Remote.GenericException):
        remote.push_changes(changes(20), lambda _: None)


def test_backoff_sleeps_and_recovers(remote, monkeypatch):
    sleeps = []
    monkeypatch.setattr(lieer.remote.time, "sleep", sleeps.append)
    execute = remote.service.users().messages().batchModify().execute
    execute.side_effect = [http_error(429)] + [None] * 30

    remote.push_changes(changes(Remote.BATCH_MODIFY_SIZE * 13), lambda _: None)

    assert sleeps and sleeps[0] == 1
    # the delay is halved again after more than 10 successful requests
    assert remote._delay == 0
