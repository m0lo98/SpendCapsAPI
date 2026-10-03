from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class Service(StrEnum):
    cloud_run = "cloud-run"
    cloud_run_functions = "cloud-run-functions"
    gemini_api = "gemini-api"
    vertex_ai = "vertex-ai"


SERVICE_IDS = {
    Service.cloud_run: "152E-C115-5142",
    Service.cloud_run_functions: "29E7-DA93-CA13",
    Service.gemini_api: "AEFD-7695-64FA",
    Service.vertex_ai: "C7E2-9256-1C43",
}


class SpendCapCreate(BaseModel):
    project_id: str = Field(min_length=1)
    service: Service
    amount: Decimal = Field(ge=0)
    currency_code: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    display_name: str | None = Field(default=None, max_length=60)


class SpendCapUpdate(BaseModel):
    amount: Decimal = Field(ge=0)


class SpendCap(BaseModel):
    id: str
    name: str
    billing_account_id: str
    display_name: str
    project: str
    service: Service | None
    service_id: str
    amount: Decimal | None
    currency_code: str | None
    state: str
    reconciling: bool
