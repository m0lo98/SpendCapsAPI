import time
from collections.abc import Iterator

import google.auth
import requests
from google.auth.transport.requests import AuthorizedSession

API_ROOT = "https://billingbudgets.googleapis.com/v1"
MAX_ATTEMPTS = 5
REQUEST_TIMEOUT = 30
SERVER_ERRORS = {500, 502, 503, 504}
LIST_PASSES = 3


class BudgetApiError(Exception):
    def __init__(self, status_code: int, message: str, status: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.status = status


class BudgetsClient:
    def __init__(self, session: AuthorizedSession | None = None):
        if session is None:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            session = AuthorizedSession(credentials)
        self.session = session

    @staticmethod
    def _is_transient(method: str, status_code: int) -> bool:
        if status_code == 429:
            return True
        # Spend cap budgets (Preview) intermittently return 404 for existing budgets.
        # POST is not retried: a failed response does not prove the budget was not created.
        return method != "POST" and (status_code == 404 or status_code in SERVER_ERRORS)

    def _call(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(MAX_ATTEMPTS):
            last_attempt = attempt == MAX_ATTEMPTS - 1
            try:
                response = self.session.request(method, f"{API_ROOT}/{path}", timeout=REQUEST_TIMEOUT, **kwargs)
            except requests.RequestException as exc:
                if method == "POST" or last_attempt:
                    status_code = 504 if isinstance(exc, requests.Timeout) else 502
                    raise BudgetApiError(status_code, f"Budget API request failed: {exc}") from exc
                time.sleep(1)
                continue
            if not self._is_transient(method, response.status_code) or last_attempt:
                break
            time.sleep(1)
        if response.status_code >= 400:
            try:
                error = response.json()["error"]
                message, status = error["message"], error.get("status", "")
            except (ValueError, KeyError, TypeError):
                message, status = response.text, ""
            raise BudgetApiError(response.status_code, message, status)
        return response.json() if response.content else {}

    def create(self, billing_account_id: str, budget: dict) -> dict:
        return self._call("POST", f"billingAccounts/{billing_account_id}/budgets", json=budget)

    def list(self, billing_account_id: str, project: str | None = None) -> Iterator[dict]:
        # Spend cap budgets (Preview) are intermittently missing from list results, so merge several passes.
        seen = set()
        for _ in range(LIST_PASSES):
            for budget in self._list_once(billing_account_id, project):
                if budget["name"] not in seen:
                    seen.add(budget["name"])
                    yield budget

    def _list_once(self, billing_account_id: str, project: str | None) -> Iterator[dict]:
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

    def patch(self, name: str, budget: dict) -> dict:
        return self._call("PATCH", name, json=budget)

    def delete(self, name: str) -> None:
        self._call("DELETE", name)
