from collections.abc import Iterator

import google.auth
from google.auth.transport.requests import AuthorizedSession

API_ROOT = "https://billingbudgets.googleapis.com/v1"


class BudgetApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class BudgetsClient:
    def __init__(self, session: AuthorizedSession | None = None):
        if session is None:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            session = AuthorizedSession(credentials)
        self.session = session

    def _call(self, method: str, path: str, **kwargs) -> dict:
        response = self.session.request(method, f"{API_ROOT}/{path}", **kwargs)
        if response.status_code >= 400:
            try:
                message = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = response.text
            raise BudgetApiError(response.status_code, message)
        return response.json() if response.content else {}

    def create(self, billing_account_id: str, budget: dict) -> dict:
        return self._call("POST", f"billingAccounts/{billing_account_id}/budgets", json=budget)

    def list(self, billing_account_id: str, project: str | None = None) -> Iterator[dict]:
        params = {"pageSize": 100}
        if project:
            params["scope"] = project
        while True:
            page = self._call("GET", f"billingAccounts/{billing_account_id}/budgets", params=dict(params))
            yield from page.get("budgets", [])
            if not page.get("nextPageToken"):
                return
            params["pageToken"] = page["nextPageToken"]

    def get(self, name: str) -> dict:
        return self._call("GET", name)

    def patch(self, name: str, budget: dict, update_mask: str) -> dict:
        return self._call("PATCH", name, json=budget, params={"updateMask": update_mask})

    def delete(self, name: str) -> None:
        self._call("DELETE", name)
