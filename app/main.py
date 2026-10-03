from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, Path, Request, Response
from fastapi.responses import JSONResponse

from app.audit import log_audit
from app.budgets_client import BudgetApiError, BudgetsClient
from app.models import SpendCap, SpendCapCreate, SpendCapUpdate
from app.service import SpendCapError, SpendCapService

app = FastAPI(title="Spend Caps API", version="0.2.0")

BillingAccountId = Annotated[str, Path(pattern=r"^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$")]
BudgetId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]


@lru_cache
def get_service() -> SpendCapService:
    return SpendCapService(BudgetsClient())


Service = Annotated[SpendCapService, Depends(get_service)]


@app.exception_handler(SpendCapError)
def _spend_cap_error(_: Request, exc: SpendCapError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(BudgetApiError)
def _budget_api_error(_: Request, exc: BudgetApiError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.get("/health")
def healthz():
    return {"status": "ok"}


@app.post("/v1/billing-accounts/{billing_account_id}/spend-caps", status_code=201)
def create_spend_cap(
    request: Request, billing_account_id: BillingAccountId, body: SpendCapCreate, svc: Service
) -> SpendCap:
    spend_cap = svc.create(billing_account_id, body)
    log_audit(
        request,
        "create",
        billing_account_id,
        spend_cap.id,
        project=spend_cap.project,
        service=body.service,
        amount=body.amount,
        currency_code=spend_cap.currency_code,
    )
    return spend_cap


@app.get("/v1/billing-accounts/{billing_account_id}/spend-caps")
def list_spend_caps(billing_account_id: BillingAccountId, svc: Service) -> list[SpendCap]:
    return svc.list_all(billing_account_id)


@app.get("/v1/billing-accounts/{billing_account_id}/spend-caps/{budget_id}")
def get_spend_cap(billing_account_id: BillingAccountId, budget_id: BudgetId, svc: Service) -> SpendCap:
    return svc.get(billing_account_id, budget_id)


@app.patch("/v1/billing-accounts/{billing_account_id}/spend-caps/{budget_id}")
def update_spend_cap(
    request: Request, billing_account_id: BillingAccountId, budget_id: BudgetId, body: SpendCapUpdate, svc: Service
) -> SpendCap:
    spend_cap = svc.update_amount(billing_account_id, budget_id, body)
    log_audit(request, "update_amount", billing_account_id, budget_id, amount=body.amount)
    return spend_cap


@app.post("/v1/billing-accounts/{billing_account_id}/spend-caps/{budget_id}:lift")
def lift_spend_cap(
    request: Request, billing_account_id: BillingAccountId, budget_id: BudgetId, svc: Service
) -> SpendCap:
    spend_cap = svc.lift(billing_account_id, budget_id)
    log_audit(request, "lift", billing_account_id, budget_id)
    return spend_cap


@app.delete("/v1/billing-accounts/{billing_account_id}/spend-caps/{budget_id}", status_code=204)
def delete_spend_cap(
    request: Request, billing_account_id: BillingAccountId, budget_id: BudgetId, svc: Service
) -> Response:
    svc.delete(billing_account_id, budget_id)
    log_audit(request, "delete", billing_account_id, budget_id)
    return Response(status_code=204)
