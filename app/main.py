from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, Path, Request, Response
from fastapi.responses import JSONResponse

from app.budgets_client import BudgetApiError, BudgetsClient
from app.models import SpendCap, SpendCapCreate, SpendCapUpdate
from app.service import SpendCapError, SpendCapService

app = FastAPI(title="Spend Caps API", version="0.2.0")

BillingAccountId = Annotated[str, Path(pattern=r"^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$")]


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


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/v1/billing-accounts/{billing_account_id}/spend-caps", status_code=201)
def create_spend_cap(billing_account_id: BillingAccountId, body: SpendCapCreate, svc: Service) -> SpendCap:
    return svc.create(billing_account_id, body)


@app.get("/v1/billing-accounts/{billing_account_id}/spend-caps")
def list_spend_caps(billing_account_id: BillingAccountId, svc: Service) -> list[SpendCap]:
    return svc.list_all(billing_account_id)


@app.get("/v1/billing-accounts/{billing_account_id}/spend-caps/{budget_id}")
def get_spend_cap(billing_account_id: BillingAccountId, budget_id: str, svc: Service) -> SpendCap:
    return svc.get(billing_account_id, budget_id)


@app.patch("/v1/billing-accounts/{billing_account_id}/spend-caps/{budget_id}")
def update_spend_cap(
    billing_account_id: BillingAccountId, budget_id: str, body: SpendCapUpdate, svc: Service
) -> SpendCap:
    return svc.update_amount(billing_account_id, budget_id, body)


@app.post("/v1/billing-accounts/{billing_account_id}/spend-caps/{budget_id}:lift")
def lift_spend_cap(billing_account_id: BillingAccountId, budget_id: str, svc: Service) -> SpendCap:
    return svc.lift(billing_account_id, budget_id)


@app.delete("/v1/billing-accounts/{billing_account_id}/spend-caps/{budget_id}", status_code=204)
def delete_spend_cap(billing_account_id: BillingAccountId, budget_id: str, svc: Service) -> Response:
    svc.delete(billing_account_id, budget_id)
    return Response(status_code=204)
