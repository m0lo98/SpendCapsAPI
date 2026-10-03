import json

import pytest

from app.budgets_client import API_ROOT, LIST_PASSES, MAX_ATTEMPTS, REQUEST_TIMEOUT, BudgetApiError, BudgetsClient


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()

    def json(self):
        return json.loads(self.content)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def test_list_follows_pagination_and_scope():
    pages = [
        FakeResponse(200, {"budgets": [{"name": "a"}], "nextPageToken": "t1"}),
        FakeResponse(200, {"budgets": [{"name": "b"}]}),
    ]
    session = FakeSession(pages * LIST_PASSES)
    names = [b["name"] for b in BudgetsClient(session).list("BA", project="projects/p")]
    assert names == ["a", "b"]
    assert session.calls[0][1] == f"{API_ROOT}/billingAccounts/BA/budgets"
    assert session.calls[0][2]["params"] == {"pageSize": 100, "scope": "projects/p"}
    assert session.calls[1][2]["params"]["pageToken"] == "t1"
    assert "pageToken" not in session.calls[2][2]["params"]


def test_list_merges_incomplete_passes():
    session = FakeSession(
        [
            FakeResponse(200, {"budgets": [{"name": "plain"}]}),
            FakeResponse(200, {"budgets": [{"name": "plain"}, {"name": "cap"}]}),
        ]
        + [FakeResponse(200, {})] * (LIST_PASSES - 2)
    )
    assert [b["name"] for b in BudgetsClient(session).list("BA")] == ["plain", "cap"]
    assert len(session.calls) == LIST_PASSES


def test_patch_sends_body_without_update_mask():
    session = FakeSession([FakeResponse(200, {"name": "n"})])
    BudgetsClient(session).patch("billingAccounts/BA/budgets/1", {"x": 1})
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("PATCH", f"{API_ROOT}/billingAccounts/BA/budgets/1")
    assert kwargs == {"json": {"x": 1}, "timeout": REQUEST_TIMEOUT}


def test_intermittent_not_found_is_retried(monkeypatch):
    monkeypatch.setattr("app.budgets_client.time.sleep", lambda _: None)
    session = FakeSession([FakeResponse(404, {"error": {"message": "nf"}}), FakeResponse(200, {"name": "n"})])
    assert BudgetsClient(session).get("billingAccounts/BA/budgets/1") == {"name": "n"}
    assert len(session.calls) == 2


def test_persistent_not_found_is_raised(monkeypatch):
    monkeypatch.setattr("app.budgets_client.time.sleep", lambda _: None)
    session = FakeSession([FakeResponse(404, {"error": {"message": "nf"}})] * MAX_ATTEMPTS)
    with pytest.raises(BudgetApiError) as exc:
        BudgetsClient(session).get("billingAccounts/BA/budgets/1")
    assert exc.value.status_code == 404
    assert len(session.calls) == MAX_ATTEMPTS


@pytest.mark.parametrize(
    "operation",
    [lambda c, n: c.get(n), lambda c, n: c.patch(n, {}), lambda c, n: c.delete(n)],
    ids=["get", "patch", "delete"],
)
@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_errors_are_retried(monkeypatch, operation, status):
    monkeypatch.setattr("app.budgets_client.time.sleep", lambda _: None)
    session = FakeSession([FakeResponse(status, {"error": {"message": "busy"}}), FakeResponse(200, {"name": "n"})])
    operation(BudgetsClient(session), "billingAccounts/BA/budgets/1")
    assert len(session.calls) == 2


def test_create_is_retried_only_on_rate_limit(monkeypatch):
    monkeypatch.setattr("app.budgets_client.time.sleep", lambda _: None)
    session = FakeSession([FakeResponse(429, {"error": {"message": "slow down"}}), FakeResponse(200, {"name": "n"})])
    assert BudgetsClient(session).create("BA", {}) == {"name": "n"}
    for status in (404, 500, 503):
        session = FakeSession([FakeResponse(status, {"error": {"message": "x"}})])
        with pytest.raises(BudgetApiError):
            BudgetsClient(session).create("BA", {})
        assert len(session.calls) == 1


def test_delete_with_empty_body():
    session = FakeSession([FakeResponse(200, None)])
    BudgetsClient(session).delete("billingAccounts/BA/budgets/1")
    assert session.calls[0][0] == "DELETE"


def test_error_message_is_extracted():
    session = FakeSession([FakeResponse(400, {"error": {"code": 400, "message": "services filter must be set"}})])
    with pytest.raises(BudgetApiError) as exc:
        BudgetsClient(session).get("billingAccounts/BA/budgets/1")
    assert exc.value.status_code == 400
    assert exc.value.message == "services filter must be set"
