from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field

DEFAULT_THRESHOLDS = [0.5, 0.9, 1.0]

Threshold = Annotated[float, Field(gt=0)]


class SpendCapCreate(BaseModel):
    project_id: str = Field(min_length=1)
    amount: Decimal = Field(gt=0)
    currency_code: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    display_name: str | None = Field(default=None, max_length=60)
    alert_thresholds: list[Threshold] = Field(default_factory=lambda: list(DEFAULT_THRESHOLDS), min_length=1)
    enforce: bool = True


class SpendCapUpdate(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0)
    alert_thresholds: list[Threshold] | None = Field(default=None, min_length=1)
    enforce: bool | None = None


class SpendCap(BaseModel):
    id: str
    name: str
    billing_account_id: str
    display_name: str
    projects: list[str]
    amount: Decimal | None
    currency_code: str | None
    alert_thresholds: list[float]
    enforced: bool


class PubSubMessage(BaseModel):
    data: str
    attributes: dict[str, str] = {}


class PubSubPush(BaseModel):
    message: PubSubMessage


class EnforcementResult(BaseModel):
    budget_id: str
    exceeded: bool
    disabled_projects: list[str] = []
