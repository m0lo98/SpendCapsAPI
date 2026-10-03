import base64
import json

import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as gexc
from google.cloud import billing_v1
from google.cloud.billing import budgets_v1

from app.config import Settings
from app.main import app, get_service
from app.service import SpendCapService

BA = "012345-6789AB-CDEF01"
TOPIC = "projects/ops/topics/spend-caps"


class FakeBudgets:
    def __init__(self):
        self.store: dict[str, budgets_v1.Budget] = {}
        self.seq = 0

    def create_budget(self, parent, budget):
        self.seq += 1
        b = budgets_v1.Budget(budget)
        b.name = f"{parent}/budgets/b{self.seq}"
        b.etag = "e1"
        self.store[b.name] = b
        return b

    def list_budgets(self, request=None, parent=None):
        parent = parent or request.parent
        scope = request.scope if request else ""
        return [
            b for n, b in self.store.items()
            if n.startswith(parent + "/") and (not scope or scope in b.budget_filter.projects)
        ]

    def get_budget(self, name):
        if name not in self.store:
            raise gexc.NotFound("budget not found")
        return self.store[name]

    def update_budget(self, budget, update_mask):
        current = self.store[budget.name]
        for path in update_mask.paths:
            setattr(current, path, getattr(budget, path))
        return current

    def delete_budget(self, name):
        self.store.pop(name)


class FakeBilling:
    def __init__(self):
        self.projects = {}

    def get_project_billing_info(self, name):
        return self.projects[name]

    def update_project_billing_info(self, name, project_billing_info):
        self.projects[name] = billing_v1.ProjectBillingInfo(
            name=name, billing_account_name=project_billing_info.billing_account_name, billing_enabled=False
        )


@pytest.fixture
def env():
    budgets, billing = FakeBudgets(), FakeBilling()
    svc = SpendCapService(Settings(pubsub_topic=TOPIC), budgets, billing)
    app.dependency_overrides[get_service] = lambda: svc
    yield TestClient(app), budgets, billing
    app.dependency_overrides.clear()


def push(budget_id, cost, limit, ba=BA):
    data = {"costAmount": cost, "budgetAmount": limit, "currencyCode": "PLN", "budgetDisplayName": "x"}
    return {
        "message": {
            "data": base64.b64encode(json.dumps(data).encode()).decode(),
            "attributes": {"billingAccountId": ba, "budgetId": budget_id, "schemaVersion": "1.0"},
        }
    }


def test_create_spend_cap(env):
    client, budgets, _ = env
    r = client.post(f"/v1/billing-accounts/{BA}/spend-caps", json={"project_id": "acme-prod", "amount": "1500.50"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["projects"] == ["projects/acme-prod"]
    assert body["display_name"] == "spend-cap-acme-prod"
    assert body["amount"] == "1500.5"
    assert body["alert_thresholds"] == [0.5, 0.9, 1.0]
    assert body["enforced"] is True

    stored = budgets.store[body["name"]]
    assert stored.notifications_rule.pubsub_topic == TOPIC
    assert stored.amount.specified_amount.units == 1500
    assert stored.amount.specified_amount.nanos == 500_000_000


def test_create_duplicate_project_conflicts(env):
    client, _, _ = env
    payload = {"project_id": "acme-prod", "amount": 100}
    assert client.post(f"/v1/billing-accounts/{BA}/spend-caps", json=payload).status_code == 201
    assert client.post(f"/v1/billing-accounts/{BA}/spend-caps", json=payload).status_code == 409


def test_create_without_enforcement_has_no_topic(env):
    client, budgets, _ = env
    r = client.post(
        f"/v1/billing-accounts/{BA}/spend-caps", json={"project_id": "p1", "amount": 10, "enforce": False}
    )
    assert r.json()["enforced"] is False
    assert budgets.store[r.json()["name"]].notifications_rule.pubsub_topic == ""


def test_enforced_create_requires_topic(env):
    client, budgets, billing = env
    app.dependency_overrides[get_service] = lambda: SpendCapService(Settings(pubsub_topic=None), budgets, billing)
    r = client.post(f"/v1/billing-accounts/{BA}/spend-caps", json={"project_id": "p1", "amount": 10})
    assert r.status_code == 500
    assert "SPEND_CAP_PUBSUB_TOPIC" in r.json()["detail"]


def test_validation(env):
    client, _, _ = env
    assert client.post("/v1/billing-accounts/bad/spend-caps", json={"project_id": "p", "amount": 1}).status_code == 422
    assert client.post(f"/v1/billing-accounts/{BA}/spend-caps", json={"project_id": "p", "amount": 0}).status_code == 422
    r = client.post(f"/v1/billing-accounts/{BA}/spend-caps", json={"project_id": "p", "amount": 1, "alert_thresholds": [-1]})
    assert r.status_code == 422


def test_list_get_update_delete(env):
    client, _, _ = env
    created = client.post(f"/v1/billing-accounts/{BA}/spend-caps", json={"project_id": "p1", "amount": 100}).json()
    base = f"/v1/billing-accounts/{BA}/spend-caps"

    assert [c["id"] for c in client.get(base).json()] == [created["id"]]
    assert client.get(f"{base}/{created['id']}").json()["amount"] == "100"

    r = client.patch(f"{base}/{created['id']}", json={"amount": 250, "alert_thresholds": [1.0, 0.8], "enforce": False})
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == "250"
    assert r.json()["alert_thresholds"] == [0.8, 1.0]
    assert r.json()["enforced"] is False

    assert client.patch(f"{base}/{created['id']}", json={}).status_code == 400
    assert client.delete(f"{base}/{created['id']}").status_code == 204
    assert client.get(f"{base}/{created['id']}").status_code == 404


def test_notification_below_limit_does_nothing(env):
    client, _, billing = env
    created = client.post(f"/v1/billing-accounts/{BA}/spend-caps", json={"project_id": "p1", "amount": 100}).json()
    billing.projects["projects/p1"] = billing_v1.ProjectBillingInfo(billing_account_name=f"billingAccounts/{BA}", billing_enabled=True)

    r = client.post("/v1/budget-notifications", json=push(created["id"], 99.99, 100))
    assert r.json() == {"budget_id": created["id"], "exceeded": False, "disabled_projects": []}
    assert billing.projects["projects/p1"].billing_enabled is True


def test_notification_over_limit_disables_billing(env):
    client, _, billing = env
    created = client.post(f"/v1/billing-accounts/{BA}/spend-caps", json={"project_id": "p1", "amount": 100}).json()
    billing.projects["projects/p1"] = billing_v1.ProjectBillingInfo(billing_account_name=f"billingAccounts/{BA}", billing_enabled=True)

    r = client.post("/v1/budget-notifications", json=push(created["id"], 100.01, 100))
    assert r.json()["disabled_projects"] == ["projects/p1"]
    assert billing.projects["projects/p1"].billing_enabled is False

    r = client.post("/v1/budget-notifications", json=push(created["id"], 120, 100))
    assert r.json()["exceeded"] is True
    assert r.json()["disabled_projects"] == []


def test_notification_ignores_unenforced_budget(env):
    client, _, billing = env
    created = client.post(
        f"/v1/billing-accounts/{BA}/spend-caps", json={"project_id": "p1", "amount": 100, "enforce": False}
    ).json()
    billing.projects["projects/p1"] = billing_v1.ProjectBillingInfo(billing_account_name=f"billingAccounts/{BA}", billing_enabled=True)

    r = client.post("/v1/budget-notifications", json=push(created["id"], 500, 100))
    assert r.json()["disabled_projects"] == []
    assert billing.projects["projects/p1"].billing_enabled is True


def test_notification_skips_project_moved_to_other_account(env):
    client, _, billing = env
    created = client.post(f"/v1/billing-accounts/{BA}/spend-caps", json={"project_id": "p1", "amount": 100}).json()
    billing.projects["projects/p1"] = billing_v1.ProjectBillingInfo(
        billing_account_name="billingAccounts/AAAAAA-BBBBBB-CCCCCC", billing_enabled=True
    )

    r = client.post("/v1/budget-notifications", json=push(created["id"], 500, 100))
    assert r.json()["disabled_projects"] == []
    assert billing.projects["projects/p1"].billing_enabled is True


def test_notification_rejects_malformed(env):
    client, _, _ = env
    bad = push("b1", 1, 1)
    bad["message"]["data"] = base64.b64encode(b"not json").decode()
    assert client.post("/v1/budget-notifications", json=bad).status_code == 400
    missing = push("b1", 1, 1)
    missing["message"]["attributes"] = {}
    assert client.post("/v1/budget-notifications", json=missing).status_code == 400
