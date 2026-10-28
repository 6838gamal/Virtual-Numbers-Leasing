"""كل Pydantic models."""
from __future__ import annotations

from pydantic import BaseModel, Field


class OrderCreate(BaseModel):
    country: str
    service: str
    voucher_code: str | None = None


class DepositRequestCreate(BaseModel):
    amount: float = Field(..., ge=5)
    method: str
    reference: str


class VoucherRedeem(BaseModel):
    code: str
    country: str | None = None
    service: str | None = None


class VoucherBatchCreate(BaseModel):
    count: int = Field(..., ge=1, le=1000)
    value: float = Field(..., ge=1)
    expires_days: int = Field(30, ge=1, le=365)
    note: str = ""


class VoucherCheck(BaseModel):
    code: str
    country: str | None = None
    service: str | None = None


class TicketCreate(BaseModel):
    subject: str
    message: str
    order_id: str | None = None


class PasswordChange(BaseModel):
    current: str
    new: str = Field(..., min_length=8)


class ProfileUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    country: str | None = None


class PreferencesUpdate(BaseModel):
    email_notifications: bool | None = None
    sms_notifications: bool | None = None
    marketing_emails: bool | None = None


class AccessCodeLogin(BaseModel):
    code: str


class AccessCodeCreate(BaseModel):
    label: str = ""
    expires_days: int = Field(30, ge=1, le=365)
    max_uses: int = Field(1, ge=1, le=100)
    preset_name: str = ""
    preset_email: str = ""
    preset_balance: float = Field(0.0, ge=0)


class PopularServiceItem(BaseModel):
    country_slug: str
    service_id: str
    order: int = 0


class PopularServicesUpdate(BaseModel):
    items: list[PopularServiceItem] = Field(default_factory=list)
