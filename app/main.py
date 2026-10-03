from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, Path, Request, Response
from fastapi.responses import JSONResponse
from google.api_core import exceptions as gexc
from google.cloud import billing_v1
from google.cloud.billing import budgets_v1

from app.config import Settings
from app.models import EnforcementResult, PubSubPush, SpendCap, SpendCapCreate, SpendCapUpdate
from app.service import SpendCapError, SpendCapService

app = FastAPI(title="Spend Caps API", version="0.1.0")

BillingAccountId = Annotated[str, Path(pattern=r"^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$")]


@lru_cache
def get_service() -> SpendCapService:
    return SpendCapService(Settings.from_env(), budgets_v1.BudgetServiceClient(), billing_v1.CloudBillingClient())


Service = Annotated[SpendCapService, Depends(get_service)]


@app.exception_handler(SpendCapError)
def _spend_cap_error(_: Request, exc: SpendCapError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(gexc.GoogleAPICallError)
def _google_error(_: Request, exc: gexc.GoogleAPICallError):
    return JSONResponse(status_code=exc.code or 502, content={"detail": exc.message})


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/v1/billing-accounts/{billing_account_id}/spend-caps", status_code=201)
def create_spend_cap(billing_account_id: BillingAccountId, body: SpendCapCreate, svc: Service) -> SpendCap:
    return svc.create(billing_account_id, body)


@app.get("/v1/billing-accounts/{billing_account_id}/spend-caps")
def list_spend_caps(billing_account_id: BillingAccountId, svc: Service) -> list[SpendCap]:
    return svc.list(billing_account_id)


@app.get("/v1/billing-accounts/{billing_account_id}/spend-caps/{budget_id}")
def get_spend_cap(billing_account_id: BillingAccountId, budget_id: str, svc: Service) -> SpendCap:
    return svc.get(billing_account_id, budget_id)


@app.patch("/v1/billing-accounts/{billing_account_id}/spend-caps/{budget_id}")
def update_spend_cap(
    billing_account_id: BillingAccountId, budget_id: str, body: SpendCapUpdate, svc: Service
) -> SpendCap:
    return svc.update(billing_account_id, budget_id, body)


@app.delete("/v1/billing-accounts/{billing_account_id}/spend-caps/{budget_id}", status_code=204)
def delete_spend_cap(billing_account_id: BillingAccountId, budget_id: str, svc: Service) -> Response:
    svc.delete(billing_account_id, budget_id)
    return Response(status_code=204)


@app.post("/v1/budget-notifications")
def budget_notification(body: PubSubPush, svc: Service) -> EnforcementResult:
    return svc.handle_notification(body)
