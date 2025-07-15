from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
import uuid
from datetime import datetime, timedelta
from sqlalchemy import select
from app_src.db.models import User, Payment, Subscription, SubscriptionPlan, PaymentStatus
from .schemas import PaymentInitialize, PaymentVerify, SubscriptionCreate
from .service import paystack_service
from app_src.db.db_connect import get_session
from app_src.auth.dependencies import get_current_user

payment_router = APIRouter()

@payment_router.post("/initialize", response_model=dict)
async def initialize_payment(
    payment_data: PaymentInitialize,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    reference = str(uuid.uuid4())
    
    # Initialize payment with Paystack
    response = await paystack_service.initialize_payment(
        email=current_user.email,
        amount=payment_data.amount,
        reference=reference,
        metadata={
            "user_id": str(current_user.id),
            "plan": payment_data.plan.value if payment_data.plan else None
        }
    )
    
    # Save payment to database
    payment = Payment(
        user_id=current_user.id,
        amount=payment_data.amount,
        currency="NGN",
        reference=reference,
        status=PaymentStatus.PENDING
    )
    session.add(payment)
    session.commit()
    
    return {
        "authorization_url": response["data"]["authorization_url"],
        "access_code": response["data"]["access_code"],
        "reference": reference
    }

@payment_router.get("/verify/{reference}", response_model=dict)
async def verify_payment(
    reference: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    # Verify payment with Paystack
    response = await paystack_service.verify_payment(reference)
    
    if response["data"]["status"] != "success":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment not successful"
        )
    
    # Update payment in database
    payment_result = await session.execute(
        select(Payment).where(Payment.reference == reference)
    )
    payment = payment_result.scalar_one_or_none()
    
    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found"
        )
    
    payment.status = PaymentStatus.SUCCESSFUL
    payment.paystack_transaction_id = response["data"]["id"]
    payment.payment_method = response["data"]["channel"]
    session.commit()
    
    # If this was for a subscription, create the subscription
    metadata = response["data"].get("metadata", {})
    plan = metadata.get("plan")
    
    if plan:
        # Determine subscription end date based on plan
        if plan == SubscriptionPlan.BASIC.value:
            end_date = datetime.utcnow() + timedelta(days=30)
        elif plan == SubscriptionPlan.PRO.value:
            end_date = datetime.utcnow() + timedelta(days=365)
        else:
            end_date = None  # For enterprise or lifetime plans
            
        # Check if user already has an active subscription
        existing_sub_result = await session.execute(
            select(Subscription)
            .where(
                Subscription.user_id == current_user.id,
                Subscription.is_active == True
            )
        )
        existing_sub = existing_sub_result.scalar_one_or_none()
        
        if existing_sub:
            existing_sub.is_active = False
            session.commit()
        
        # Create new subscription
        subscription = Subscription(
            user_id=current_user.id,
            plan=SubscriptionPlan(plan),
            is_active=True,
            start_date=datetime.utcnow(),
            end_date=end_date,
            paystack_customer_code=response["data"]["customer"]["customer_code"]
        )
        session.add(subscription)
        session.commit()
    
    return {"status": "success", "message": "Payment verified successfully"}

@payment_router.post("/subscriptions", response_model=dict)
async def create_subscription(
    subscription_data: SubscriptionCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    # First create a payment authorization
    reference = str(uuid.uuid4())
    
    response = await paystack_service.initialize_payment(
        email=current_user.email,
        amount=0,  # For subscription, amount is determined by the plan
        reference=reference,
        metadata={
            "user_id": str(current_user.id),
            "plan": subscription_data.plan.value,
            "is_subscription": True,
            "authorization": True
        }
    )
    
    return {
        "authorization_url": response["data"]["authorization_url"],
        "access_code": response["data"]["access_code"],
        "reference": reference
    }

@payment_router.post("/webhook")
async def paystack_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    payload = await request.json()
    event = payload.get("event")
    
    if event == "charge.success":
        data = payload.get("data")
        reference = data.get("reference")
        
        # Verify the transaction is valid
        paystack_response = await paystack_service.verify_payment(reference)
        
        if paystack_response["data"]["status"] == "success":
            # Update payment in database
            payment_result = await session.execute(
                select(Payment).where(Payment.reference == reference)
            )
            payment = payment_result.scalar_one_or_none()
            if payment:
                payment.status = PaymentStatus.SUCCESSFUL
                payment.paystack_transaction_id = data["id"]
                payment.payment_method = data["channel"]
                session.commit()
                
                # Handle subscription if this was for a subscription
                metadata = data.get("metadata", {})
                if metadata.get("is_subscription"):
                    authorization_code = data["authorization"]["authorization_code"]
                    plan_code = metadata.get("plan_code")
                    
                    # Create subscription with Paystack
                    subscription_response = await paystack_service.create_subscription(
                        customer_email=data["customer"]["email"],
                        plan_code=plan_code,
                        authorization_code=authorization_code
                    )
                    
                    # Save subscription to database
                    subscription = Subscription(
                        user_id=payment.user_id,
                        plan=SubscriptionPlan(metadata.get("plan")),
                        is_active=True,
                        start_date=datetime.utcnow(),
                        paystack_subscription_id=subscription_response["data"]["subscription_code"],
                        paystack_customer_code=data["customer"]["customer_code"]
                    )
                    session.add(subscription)
                    session.commit()
    
    return {"status": "success"}