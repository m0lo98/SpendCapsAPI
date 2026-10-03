from decimal import Decimal

from app.budgets_client import LIST_PASSES, BudgetApiError, BudgetsClient
from app.models import SERVICE_IDS, SERVICE_NAMES, SpendCap, SpendCapCreate, SpendCapUpdate

SERVICES_BY_ID = {service_id: service for service, service_id in SERVICE_IDS.items()}
SPEND_CAP_THRESHOLDS = [0.5, 0.8, 1.0]
DUPLICATE_LOOKUP_PASSES = 8
DELETE_ATTEMPTS = 3
DEFAULT_DISPLAY_NAME_FORMAT = "spend-cap-{project_id}-{service}"


class SpendCapError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _to_money(amount: Decimal, currency_code: str | None = None) -> dict:
    units = int(amount)
    nanos = int(((amount - units) * 1_000_000_000).to_integral_value())
    money = {"units": str(units), "nanos": nanos}
    if currency_code:
        money["currencyCode"] = currency_code
    return money


def _from_money(money: dict) -> Decimal:
    return Decimal(money.get("units", "0")) + Decimal(money.get("nanos", 0)) / Decimal(1_000_000_000)


def _to_spend_cap(budget: dict) -> SpendCap:
    _, billing_account_id, _, budget_id = budget["name"].split("/")
    budget_filter = budget.get("budgetFilter", {})
    service_id = (budget_filter.get("services") or [""])[0].removeprefix("services/")
    specified = budget.get("amount", {}).get("specifiedAmount")
    spend_cap = budget.get("spendCap", {})
    return SpendCap(
        id=budget_id,
        name=budget["name"],
        billing_account_id=billing_account_id,
        display_name=budget.get("displayName", ""),
        project=(budget_filter.get("projects") or [""])[0],
        service=SERVICES_BY_ID.get(service_id),
        service_id=service_id,
        amount=_from_money(specified) if specified is not None else None,
        currency_code=specified.get("currencyCode") if specified is not None else None,
        state=spend_cap.get("outputState", "STATE_UNSPECIFIED"),
        reconciling=spend_cap.get("reconciling", False),
    )


class SpendCapService:
    def __init__(self, budgets: BudgetsClient, display_name_format: str = DEFAULT_DISPLAY_NAME_FORMAT):
        self.budgets = budgets
        self.display_name_format = display_name_format

    def _display_name(self, req: SpendCapCreate) -> str:
        return self.display_name_format.format(
            project_id=req.project_id, service=req.service.value, service_name=SERVICE_NAMES[req.service]
        )[:60]

    @staticmethod
    def _budget_name(billing_account_id: str, budget_id: str) -> str:
        return f"billingAccounts/{billing_account_id}/budgets/{budget_id}"

    def _get_spend_cap_budget(self, billing_account_id: str, budget_id: str) -> dict:
        budget = self.budgets.get(self._budget_name(billing_account_id, budget_id))
        if "spendCap" not in budget:
            raise SpendCapError(404, f"Budget {budget_id} is not a spend cap budget")
        return budget

    def _find_cap_for(
        self, billing_account_id: str, project: str, service: str, passes: int = LIST_PASSES
    ) -> dict | None:
        for budget in self.budgets.list(billing_account_id, project=project, passes=passes):
            if "spendCap" in budget and service in budget.get("budgetFilter", {}).get("services", []):
                return budget
        return None

    @staticmethod
    def _duplicate_error(project: str, service: str, existing: dict) -> SpendCapError:
        return SpendCapError(409, f"{project} already has a spend cap for {service}: {existing['name']}")

    def create(self, billing_account_id: str, req: SpendCapCreate) -> SpendCap:
        project = f"projects/{req.project_id}"
        service = f"services/{SERVICE_IDS[req.service]}"
        existing = self._find_cap_for(billing_account_id, project, service)
        if existing:
            raise self._duplicate_error(project, service, existing)
        budget = {
            "displayName": req.display_name or self._display_name(req),
            "budgetFilter": {
                "projects": [project],
                "services": [service],
                "creditTypesTreatment": "EXCLUDE_ALL_CREDITS",
                "calendarPeriod": "MONTH",
            },
            "amount": {"specifiedAmount": _to_money(req.amount, req.currency_code)},
            "thresholdRules": [{"thresholdPercent": t, "spendBasis": "CURRENT_SPEND"} for t in SPEND_CAP_THRESHOLDS],
            "notificationsRule": {"enableProjectLevelRecipients": True},
            "spendCap": {"inputState": "CONFIGURED"},
            "ownershipScope": "ALL_USERS",
        }
        try:
            created = self.budgets.create(billing_account_id, budget)
        except BudgetApiError as exc:
            # GCP rejects a second cap for the same project and service with a bare 400 INVALID_ARGUMENT.
            if exc.status_code != 400:
                raise
            existing = self._find_cap_for(billing_account_id, project, service, passes=DUPLICATE_LOOKUP_PASSES)
            if existing is None:
                raise
            raise self._duplicate_error(project, service, existing) from exc
        return _to_spend_cap(created)

    def list_all(self, billing_account_id: str) -> list[SpendCap]:
        return [_to_spend_cap(b) for b in self.budgets.list(billing_account_id) if "spendCap" in b]

    def get(self, billing_account_id: str, budget_id: str) -> SpendCap:
        return _to_spend_cap(self._get_spend_cap_budget(billing_account_id, budget_id))

    @staticmethod
    def _full_budget(budget: dict, input_state: str) -> dict:
        body = dict(budget)
        body["ownershipScope"] = "ALL_USERS"
        body["spendCap"] = {"inputState": input_state}
        return body

    def _patch(self, budget_id: str, body: dict) -> SpendCap:
        try:
            return _to_spend_cap(self.budgets.patch(body["name"], body))
        except BudgetApiError as exc:
            if exc.status == "FAILED_PRECONDITION":
                raise SpendCapError(409, f"Spend cap {budget_id} changed since it was read; retry the request") from exc
            raise

    def update_amount(self, billing_account_id: str, budget_id: str, req: SpendCapUpdate) -> SpendCap:
        budget = self._get_spend_cap_budget(billing_account_id, budget_id)
        spend_cap = budget["spendCap"]
        if spend_cap.get("outputState") == "ENFORCED":
            raise SpendCapError(409, f"Spend cap {budget_id} is enforced; lift it before changing the amount")
        body = self._full_budget(budget, input_state=spend_cap.get("inputState", "CONFIGURED"))
        currency = budget.get("amount", {}).get("specifiedAmount", {}).get("currencyCode")
        body["amount"] = {"specifiedAmount": _to_money(req.amount, currency)}
        return self._patch(budget_id, body)

    def lift(self, billing_account_id: str, budget_id: str) -> SpendCap:
        budget = self._get_spend_cap_budget(billing_account_id, budget_id)
        if budget["spendCap"].get("outputState") != "ENFORCED":
            raise SpendCapError(409, f"Spend cap {budget_id} is not enforced")
        body = self._full_budget(budget, input_state="AWAITING_NEXT_PERIOD")
        return self._patch(budget_id, body)

    def _exists(self, name: str) -> bool:
        try:
            self.budgets.get(name)
        except BudgetApiError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    def delete(self, billing_account_id: str, budget_id: str) -> None:
        name = self._get_spend_cap_budget(billing_account_id, budget_id)["name"]
        # Spend cap budgets (Preview) can survive a DELETE that returned success.
        for _ in range(DELETE_ATTEMPTS):
            try:
                self.budgets.delete(name)
            except BudgetApiError as exc:
                if exc.status_code != 404:
                    raise
            if not self._exists(name):
                return
        raise SpendCapError(502, f"Spend cap {budget_id} still exists after {DELETE_ATTEMPTS} delete attempts")
