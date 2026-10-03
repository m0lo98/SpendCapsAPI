from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


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

SERVICE_NAMES = {
    Service.cloud_run: "Cloud Run",
    Service.cloud_run_functions: "Cloud Run functions",
    Service.gemini_api: "Gemini API",
    Service.vertex_ai: "Vertex AI",
}

MAX_MONEY_UNITS = 2**63 - 1
Amount = Annotated[Decimal, Field(ge=0, le=MAX_MONEY_UNITS, decimal_places=2)]


class SpendCapCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    service: Service
    amount: Amount
    currency_code: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    display_name: str | None = Field(default=None, max_length=60)


class SpendCapUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Amount


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
