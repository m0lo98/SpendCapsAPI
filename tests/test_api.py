import copy

import pytest
from fastapi.testclient import TestClient

from app.budgets_client import BudgetApiError
from app.main import app, get_service
from app.service import SpendCapService

BA = "012345-6789AB-CDEF01"
BASE = f"/v1/billing-accounts/{BA}/spend-caps"
CLOUD_RUN = "services/152E-C115-5142"


class FakeBudgets:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.patches: list[tuple[str, dict]] = []
        self.seq = 0

    def create(self, billing_account_id, budget):
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

    def list(self, billing_account_id, project=None):
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


def test_create_duplicate_for_same_service_conflicts(env):
    client, _ = env
    assert create(client).status_code == 201
    assert create(client).status_code == 409
    assert create(client, service="gemini-api").status_code == 201


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
    assert create(client, project_id="Acme prod").status_code == 422
    assert client.get(f"{BASE}/bad.id").status_code == 422
    assert client.patch(f"{BASE}/b1", json={"amount": "1.999"}).status_code == 422


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


def test_get_and_delete(env):
    client, _ = env
    created = create(client).json()
    assert client.get(f"{BASE}/{created['id']}").json()["id"] == created["id"]
    assert client.delete(f"{BASE}/{created['id']}").status_code == 204
    assert client.get(f"{BASE}/{created['id']}").status_code == 404


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
    assert "etag" not in body


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
