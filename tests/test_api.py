import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.budgets_client import BudgetApiError
from app.main import app, get_service
from app.service import DELETE_ATTEMPTS, DUPLICATE_LOOKUP_PASSES, SpendCapService
from tests.test_audit import id_token

BA = "012345-6789AB-CDEF01"
BASE = f"/v1/billing-accounts/{BA}/spend-caps"
CLOUD_RUN = "services/152E-C115-5142"


class FakeBudgets:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.patches: list[tuple[str, dict]] = []
        self.seq = 0
        self.list_passes: list[int | None] = []

    def create(self, billing_account_id, budget):
        if "spendCap" in budget and any(
            "spendCap" in b and b["budgetFilter"] == budget["budgetFilter"] for b in self.store.values()
        ):
            raise BudgetApiError(400, "Request contains an invalid argument.", "INVALID_ARGUMENT")
        self.seq += 1
        b = copy.deepcopy(budget)
        b["name"] = f"billingAccounts/{billing_account_id}/budgets/b{self.seq}"
        b["etag"] = "e1"
        if "spendCap" in b:
            b["spendCap"] = {"inputState": "CONFIGURED", "outputState": "CONFIGURED"}
        if "currencyCode" not in b["amount"]["specifiedAmount"]:
            b["amount"]["specifiedAmount"]["currencyCode"] = "PLN"
        self.store[b["name"]] = b
        return b

    def list(self, billing_account_id, project=None, passes=None):
        self.list_passes.append(passes)
        prefix = f"billingAccounts/{billing_account_id}/"
        return [
            b
            for n, b in self.store.items()
            if n.startswith(prefix) and (project is None or project in b["budgetFilter"].get("projects", []))
        ]

    def get(self, name):
        if name not in self.store:
            raise BudgetApiError(404, "Budget not found")
        return self.store[name]

    def patch(self, name, budget):
        self.patches.append((name, budget))
        current = self.store[name]
        if budget.get("etag") != current.get("etag"):
            raise BudgetApiError(400, "Precondition check failed.", "FAILED_PRECONDITION")
        current["etag"] = f"e{len(self.patches) + 1}"
        current["amount"] = budget["amount"]
        state = budget["spendCap"]["inputState"]
        current["spendCap"] = {"inputState": state, "outputState": state}
        return current

    def delete(self, name):
        self.store.pop(name)


@pytest.fixture
def env():
    budgets = FakeBudgets()
    app.dependency_overrides[get_service] = lambda: SpendCapService(budgets)
    yield TestClient(app), budgets
    app.dependency_overrides.clear()


def create(client, **overrides):
    return client.post(BASE, json={"project_id": "acme-prod", "service": "cloud-run", "amount": "1500.50", **overrides})


def test_create_builds_spend_cap_budget(env):
    client, budgets = env
    r = create(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["project"] == "projects/acme-prod"
    assert body["service"] == "cloud-run"
    assert body["service_id"] == "152E-C115-5142"
    assert body["amount"] == "1500.5"
    assert body["currency_code"] == "PLN"
    assert body["state"] == "CONFIGURED"
    assert body["display_name"] == "spend-cap-acme-prod-cloud-run"

    sent = budgets.store[body["name"]]
    assert sent["budgetFilter"] == {
        "projects": ["projects/acme-prod"],
        "services": [CLOUD_RUN],
        "creditTypesTreatment": "EXCLUDE_ALL_CREDITS",
        "calendarPeriod": "MONTH",
    }
    assert sent["amount"]["specifiedAmount"]["units"] == "1500"
    assert sent["amount"]["specifiedAmount"]["nanos"] == 500_000_000
    assert [r["thresholdPercent"] for r in sent["thresholdRules"]] == [0.5, 0.8, 1.0]
    assert {r["spendBasis"] for r in sent["thresholdRules"]} == {"CURRENT_SPEND"}
    assert sent["notificationsRule"] == {"enableProjectLevelRecipients": True}
    assert sent["ownershipScope"] == "ALL_USERS"


def test_create_passes_currency(env):
    client, budgets = env
    r = create(client, currency_code="EUR")
    assert budgets.store[r.json()["name"]]["amount"]["specifiedAmount"]["currencyCode"] == "EUR"


def create_with_display_name_format(display_name_format, **overrides):
    budgets = FakeBudgets()
    app.dependency_overrides[get_service] = lambda: SpendCapService(budgets, display_name_format)
    try:
        return create(TestClient(app), **overrides)
    finally:
        app.dependency_overrides.clear()


def test_create_uses_display_name_format():
    r = create_with_display_name_format("[SpendCaps] [{service_name}] {project_id}")
    assert r.json()["display_name"] == "[SpendCaps] [Cloud Run] acme-prod"


def test_create_truncates_formatted_display_name():
    r = create_with_display_name_format("{project_id} " * 7)
    assert r.json()["display_name"] == ("acme-prod " * 7)[:60]


def test_create_explicit_display_name_overrides_format():
    r = create_with_display_name_format("[SpendCaps] {project_id}", display_name="ACME cap")
    assert r.json()["display_name"] == "ACME cap"


def test_create_duplicate_for_same_service_conflicts(env):
    client, _ = env
    assert create(client).status_code == 201
    assert create(client).status_code == 409
    assert create(client, service="gemini-api").status_code == 201


def test_create_duplicate_missed_by_list_conflicts(env):
    client, budgets = env
    existing = create(client).json()
    real_list = budgets.list
    misses = iter([True])
    budgets.list = lambda *args, **kwargs: [] if next(misses, False) else real_list(*args, **kwargs)
    r = create(client)
    assert r.status_code == 409
    assert existing["name"] in r.json()["detail"]
    assert budgets.list_passes[-1] == DUPLICATE_LOOKUP_PASSES


def test_create_invalid_argument_without_duplicate_is_passed_through(env):
    client, budgets = env

    def reject(*_args, **_kwargs):
        raise BudgetApiError(400, "Request contains an invalid argument.", "INVALID_ARGUMENT")

    budgets.create = reject
    r = create(client)
    assert r.status_code == 400
    assert r.json()["detail"] == "Request contains an invalid argument."


def test_create_ignores_regular_budget_on_same_project(env):
    client, budgets = env
    budgets.store[f"billingAccounts/{BA}/budgets/plain"] = {
        "name": f"billingAccounts/{BA}/budgets/plain",
        "budgetFilter": {"projects": ["projects/acme-prod"], "services": [CLOUD_RUN]},
    }
    assert create(client).status_code == 201


def test_validation(env):
    client, _ = env
    assert (
        client.post(
            "/v1/billing-accounts/bad/spend-caps", json={"project_id": "p", "service": "cloud-run", "amount": 1}
        ).status_code
        == 422
    )
    assert create(client, service="bigquery").status_code == 422
    assert create(client, amount=-1).status_code == 422
    assert create(client, currency_code="pln").status_code == 422
    assert create(client, amount="1.999").status_code == 422
    assert create(client, amount="1e30").status_code == 422
    assert create(client, project_id="Acme prod").status_code == 422
    assert client.get(f"{BASE}/bad.id").status_code == 422
    assert client.patch(f"{BASE}/b1", json={"amount": "1.999"}).status_code == 422
    assert client.patch(f"{BASE}/b1", json={"amount": "1e30"}).status_code == 422


def test_health(env):
    client, _ = env
    assert client.get("/health").json() == {"status": "ok"}


def test_list_only_returns_spend_caps(env):
    client, budgets = env
    created = create(client).json()
    budgets.store[f"billingAccounts/{BA}/budgets/plain"] = {
        "name": f"billingAccounts/{BA}/budgets/plain",
        "budgetFilter": {},
    }
    assert [c["id"] for c in client.get(BASE).json()] == [created["id"]]


def test_list_tolerates_cap_without_filter_entries(env):
    client, budgets = env
    budgets.store[f"billingAccounts/{BA}/budgets/odd"] = {
        "name": f"billingAccounts/{BA}/budgets/odd",
        "budgetFilter": {"projects": [], "services": []},
        "spendCap": {"outputState": "CONFIGURED"},
    }
    r = client.get(BASE)
    assert r.status_code == 200, r.text
    assert r.json()[0]["project"] == ""
    assert r.json()[0]["service"] is None


def test_get_and_delete(env):
    client, _ = env
    created = create(client).json()
    assert client.get(f"{BASE}/{created['id']}").json()["id"] == created["id"]
    assert client.delete(f"{BASE}/{created['id']}").status_code == 204
    assert client.get(f"{BASE}/{created['id']}").status_code == 404


def ignore_deletes(budgets, times):
    real_delete = budgets.delete
    calls = []

    def delete(name):
        calls.append(name)
        if len(calls) > times:
            real_delete(name)

    budgets.delete = delete
    return calls


def test_delete_is_retried_until_the_cap_is_gone(env):
    client, budgets = env
    created = create(client).json()
    calls = ignore_deletes(budgets, times=1)
    assert client.delete(f"{BASE}/{created['id']}").status_code == 204
    assert len(calls) == 2
    assert created["name"] not in budgets.store


def test_delete_that_never_takes_effect_fails(env):
    client, budgets = env
    created = create(client).json()
    calls = ignore_deletes(budgets, times=DELETE_ATTEMPTS)
    r = client.delete(f"{BASE}/{created['id']}")
    assert r.status_code == 502
    assert "still exists" in r.json()["detail"]
    assert len(calls) == DELETE_ATTEMPTS
    assert created["name"] in budgets.store


def test_delete_not_found_counts_as_deleted_when_cap_is_gone(env):
    client, budgets = env
    created = create(client).json()

    def delete_then_not_found(name):
        budgets.store.pop(name)
        raise BudgetApiError(404, "Budget not found")

    budgets.delete = delete_then_not_found
    assert client.delete(f"{BASE}/{created['id']}").status_code == 204


def test_regular_budget_is_not_exposed(env):
    client, budgets = env
    budgets.store[f"billingAccounts/{BA}/budgets/plain"] = {"name": f"billingAccounts/{BA}/budgets/plain"}
    assert client.get(f"{BASE}/plain").status_code == 404
    assert client.delete(f"{BASE}/plain").status_code == 404
    assert f"billingAccounts/{BA}/budgets/plain" in budgets.store


def test_update_amount_sends_full_budget(env):
    client, budgets = env
    created = create(client).json()
    r = client.patch(f"{BASE}/{created['id']}", json={"amount": 250})
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == "250"
    assert r.json()["state"] == "CONFIGURED"
    _, body = budgets.patches[-1]
    assert body["amount"]["specifiedAmount"] == {"units": "250", "nanos": 0, "currencyCode": "PLN"}
    assert body["ownershipScope"] == "ALL_USERS"
    assert body["spendCap"] == {"inputState": "CONFIGURED"}
    assert body["budgetFilter"]["services"] == [CLOUD_RUN]
    assert body["etag"] == "e1"


def test_update_amount_rejects_concurrent_change(env):
    client, budgets = env
    created = create(client).json()
    stale = copy.deepcopy(budgets.store[created["name"]]) | {"etag": "e0"}
    budgets.get = lambda _name: stale
    r = client.patch(f"{BASE}/{created['id']}", json={"amount": 250})
    assert r.status_code == 409
    assert "changed since it was read" in r.json()["detail"]
    assert budgets.store[created["name"]]["amount"]["specifiedAmount"]["units"] == "1500"


def test_update_amount_keeps_lifted_state_while_reconciling(env):
    client, budgets = env
    created = create(client).json()
    budgets.store[created["name"]]["spendCap"] = {
        "inputState": "AWAITING_NEXT_PERIOD",
        "outputState": "CONFIGURED",
        "reconciling": True,
    }
    client.patch(f"{BASE}/{created['id']}", json={"amount": 250})
    assert budgets.patches[-1][1]["spendCap"] == {"inputState": "AWAITING_NEXT_PERIOD"}


def test_update_amount_rejected_while_enforced(env):
    client, budgets = env
    created = create(client).json()
    budgets.store[created["name"]]["spendCap"]["outputState"] = "ENFORCED"
    assert client.patch(f"{BASE}/{created['id']}", json={"amount": 250}).status_code == 409
    assert budgets.patches == []


def test_lift_requires_enforced_state(env):
    client, _ = env
    created = create(client).json()
    assert client.post(f"{BASE}/{created['id']}:lift").status_code == 409


def test_lift_enforced_cap(env):
    client, budgets = env
    created = create(client).json()
    budgets.store[created["name"]]["spendCap"]["outputState"] = "ENFORCED"
    r = client.post(f"{BASE}/{created['id']}:lift")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "AWAITING_NEXT_PERIOD"
    _, body = budgets.patches[-1]
    assert body["spendCap"] == {"inputState": "AWAITING_NEXT_PERIOD"}
    assert body["ownershipScope"] == "ALL_USERS"


def test_google_api_error_is_passed_through(env):
    client, budgets = env

    def fail(*_args, **_kwargs):
        raise BudgetApiError(403, "Permission billing.budgets.configureSpendCap denied")

    budgets.create = fail
    r = create(client)
    assert r.status_code == 403
    assert "configureSpendCap" in r.json()["detail"]


def test_unknown_fields_are_rejected(env):
    client, _ = env
    assert create(client, currency="EUR").status_code == 422
    created = create(client).json()
    assert client.patch(f"{BASE}/{created['id']}", json={"amount": 250, "currency": "EUR"}).status_code == 422


def test_create_tolerates_response_without_spend_cap(env):
    client, budgets = env
    original_create = budgets.create

    def create_without_spend_cap(billing_account_id, budget):
        created = original_create(billing_account_id, budget)
        return {k: v for k, v in created.items() if k != "spendCap"}

    budgets.create = create_without_spend_cap
    r = create(client)
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "STATE_UNSPECIFIED"


@pytest.mark.parametrize(("upstream", "expected"), [(401, 502), (500, 502), (403, 403), (503, 503)])
def test_upstream_status_mapping(env, upstream, expected):
    client, budgets = env

    def fail(*_args, **_kwargs):
        raise BudgetApiError(upstream, "upstream failure")

    budgets.get = fail
    assert client.get(f"{BASE}/b1").status_code == expected


def audit_entries(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]


def test_mutations_are_audited_with_caller(env, capsys):
    client, budgets = env
    client.headers["Authorization"] = f"Bearer {id_token({'email': 'ops@example.org'})}"
    created = create(client).json()
    client.patch(f"{BASE}/{created['id']}", json={"amount": 250})
    budgets.store[created["name"]]["spendCap"]["outputState"] = "ENFORCED"
    client.post(f"{BASE}/{created['id']}:lift")
    client.delete(f"{BASE}/{created['id']}")

    entries = audit_entries(capsys)
    assert [e["audit"]["action"] for e in entries] == ["create", "update_amount", "lift", "delete"]
    assert {e["audit"]["caller"] for e in entries} == {"ops@example.org"}
    assert {e["audit"]["spend_cap_id"] for e in entries} == {created["id"]}
    assert {e["severity"] for e in entries} == {"NOTICE"}
    assert entries[0]["audit"] | {"caller": None} == {
        "caller": None,
        "action": "create",
        "billing_account_id": created["billing_account_id"],
        "spend_cap_id": created["id"],
        "project": "projects/acme-prod",
        "service": "cloud-run",
        "amount": "1500.50",
        "currency_code": "PLN",
    }
    assert entries[1]["audit"]["amount"] == "250"


def test_reads_and_failed_mutations_are_not_audited(env, capsys):
    client, _ = env
    created = create(client).json()
    capsys.readouterr()
    client.get(BASE)
    client.get(f"{BASE}/{created['id']}")
    client.post(f"{BASE}/{created['id']}:lift")
    client.delete(f"{BASE}/missing")
    assert audit_entries(capsys) == []
