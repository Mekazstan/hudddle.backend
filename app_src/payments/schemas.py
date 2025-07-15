from pydantic import BaseModel
from typing import Optional

from app_src.db.models import SubscriptionPlan

class PaymentInitialize(BaseModel):
    amount: float
    plan: Optional[SubscriptionPlan] = None

class PaymentVerify(BaseModel):
    reference: str

class SubscriptionCreate(BaseModel):
    plan: SubscriptionPlan