import base64
import json
from decimal import Decimal

from google.cloud import billing_v1
from google.cloud.billing import budgets_v1
from google.protobuf import field_mask_pb2
from google.type import money_pb2

from app.config import Settings
from app.models import (
    EnforcementResult,
    PubSubPush,
    SpendCap,
    SpendCapCreate,
    SpendCapUpdate,
)


class SpendCapError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _to_money(amount: Decimal, currency_code: str | None) -> money_pb2.Money:
    units = int(amount)
    nanos = int(((amount - units) * 1_000_000_000).to_integral_value())
    return money_pb2.Money(currency_code=currency_code or "", units=units, nanos=nanos)


def _from_money(money) -> Decimal:
    return Decimal(money.units) + Decimal(money.nanos) / Decimal(1_000_000_000)


class SpendCapService:
    def __init__(
        self,
        settings: Settings,
        budgets: budgets_v1.BudgetServiceClient,
        billing: billing_v1.CloudBillingClient,
    ):
        self.settings = settings
        self.budgets = budgets
        self.billing = billing

    def _budget_name(self, billing_account_id: str, budget_id: str) -> str:
        return f"billingAccounts/{billing_account_id}/budgets/{budget_id}"

    def _notifications_rule(self, enforce: bool) -> budgets_v1.NotificationsRule:
        if not enforce:
            return budgets_v1.NotificationsRule()
        if not self.settings.pubsub_topic:
            raise SpendCapError(
                500, "SPEND_CAP_PUBSUB_TOPIC is not configured; enforced spend caps are unavailable"
            )
        return budgets_v1.NotificationsRule(pubsub_topic=self.settings.pubsub_topic, schema_version="1.0")

    @staticmethod
    def _threshold_rules(thresholds: list[float]) -> list[budgets_v1.ThresholdRule]:
        return [
            budgets_v1.ThresholdRule(threshold_percent=t, spend_basis=budgets_v1.ThresholdRule.Basis.CURRENT_SPEND)
            for t in sorted(set(thresholds))
        ]

    def _is_enforced(self, budget: budgets_v1.Budget) -> bool:
        topic = budget.notifications_rule.pubsub_topic
        return bool(topic) and topic == self.settings.pubsub_topic

    def _to_spend_cap(self, budget: budgets_v1.Budget) -> SpendCap:
        parts = budget.name.split("/")
        specified = budget.amount.specified_amount if "specified_amount" in budget.amount else None
        return SpendCap(
            id=parts[3],
            name=budget.name,
            billing_account_id=parts[1],
            display_name=budget.display_name,
            projects=list(budget.budget_filter.projects),
            amount=_from_money(specified) if specified is not None else None,
            currency_code=(specified.currency_code or None) if specified is not None else None,
            alert_thresholds=[r.threshold_percent for r in budget.threshold_rules],
            enforced=self._is_enforced(budget),
        )

    def _ensure_no_budget_for_project(self, billing_account_id: str, project_id: str) -> None:
        request = budgets_v1.ListBudgetsRequest(
            parent=f"billingAccounts/{billing_account_id}", scope=f"projects/{project_id}"
        )
        existing = next(iter(self.budgets.list_budgets(request=request)), None)
        if existing:
            raise SpendCapError(409, f"Project {project_id} already has a budget: {existing.name}")

    def create(self, billing_account_id: str, req: SpendCapCreate) -> SpendCap:
        self._ensure_no_budget_for_project(billing_account_id, req.project_id)

        budget = budgets_v1.Budget(
            display_name=req.display_name or f"spend-cap-{req.project_id}"[:60],
            budget_filter=budgets_v1.Filter(
                projects=[f"projects/{req.project_id}"],
                credit_types_treatment=budgets_v1.Filter.CreditTypesTreatment.INCLUDE_ALL_CREDITS,
            ),
            amount=budgets_v1.BudgetAmount(specified_amount=_to_money(req.amount, req.currency_code)),
            threshold_rules=self._threshold_rules(req.alert_thresholds),
            notifications_rule=self._notifications_rule(req.enforce),
        )
        created = self.budgets.create_budget(parent=f"billingAccounts/{billing_account_id}", budget=budget)
        return self._to_spend_cap(created)

    def list_all(self, billing_account_id: str) -> list[SpendCap]:
        return [self._to_spend_cap(b) for b in self.budgets.list_budgets(parent=f"billingAccounts/{billing_account_id}")]

    def get(self, billing_account_id: str, budget_id: str) -> SpendCap:
        return self._to_spend_cap(self.budgets.get_budget(name=self._budget_name(billing_account_id, budget_id)))

    def update(self, billing_account_id: str, budget_id: str, req: SpendCapUpdate) -> SpendCap:
        current = self.budgets.get_budget(name=self._budget_name(billing_account_id, budget_id))
        budget = budgets_v1.Budget(name=current.name, etag=current.etag)
        paths = []
        if req.amount is not None:
            currency = current.amount.specified_amount.currency_code or None
            budget.amount = budgets_v1.BudgetAmount(specified_amount=_to_money(req.amount, currency))
            paths.append("amount")
        if req.alert_thresholds is not None:
            budget.threshold_rules = self._threshold_rules(req.alert_thresholds)
            paths.append("threshold_rules")
        if req.enforce is not None:
            budget.notifications_rule = self._notifications_rule(req.enforce)
            paths.append("notifications_rule")
        if not paths:
            raise SpendCapError(400, "Nothing to update")
        updated = self.budgets.update_budget(budget=budget, update_mask=field_mask_pb2.FieldMask(paths=paths))
        return self._to_spend_cap(updated)

    def delete(self, billing_account_id: str, budget_id: str) -> None:
        self.budgets.delete_budget(name=self._budget_name(billing_account_id, budget_id))

    @staticmethod
    def _parse_notification(push: PubSubPush) -> tuple[str, str, Decimal, Decimal]:
        attrs = push.message.attributes
        billing_account_id = attrs.get("billingAccountId")
        budget_id = attrs.get("budgetId")
        if not billing_account_id or not budget_id:
            raise SpendCapError(400, "Missing billingAccountId or budgetId attribute")
        try:
            payload = json.loads(base64.b64decode(push.message.data))
            cost = Decimal(str(payload["costAmount"]))
            limit = Decimal(str(payload["budgetAmount"]))
        except (ValueError, KeyError, TypeError) as e:
            raise SpendCapError(400, f"Invalid budget notification payload: {e}") from e
        return billing_account_id, budget_id, cost, limit

    def _disable_billing(self, billing_account_id: str, projects: list[str]) -> list[str]:
        disabled = []
        for project in projects:
            info = self.billing.get_project_billing_info(name=project)
            if info.billing_enabled and info.billing_account_name == f"billingAccounts/{billing_account_id}":
                self.billing.update_project_billing_info(
                    name=project, project_billing_info=billing_v1.ProjectBillingInfo(billing_account_name="")
                )
                disabled.append(project)
        return disabled

    def handle_notification(self, push: PubSubPush) -> EnforcementResult:
        billing_account_id, budget_id, cost, limit = self._parse_notification(push)
        if cost < limit:
            return EnforcementResult(budget_id=budget_id, exceeded=False)

        budget = self.budgets.get_budget(name=self._budget_name(billing_account_id, budget_id))
        if not self._is_enforced(budget):
            return EnforcementResult(budget_id=budget_id, exceeded=True)

        disabled = self._disable_billing(billing_account_id, list(budget.budget_filter.projects))
        return EnforcementResult(budget_id=budget_id, exceeded=True, disabled_projects=disabled)
