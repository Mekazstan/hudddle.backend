import httpx
from fastapi import HTTPException
from app_src.config import Config

PAYSTACK_SECRET_KEY = Config.PAYSTACK_SECRET_KEY
PAYSTACK_BASE_URL = Config.PAYSTACK_BASE_URL

class PaystackService:
    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json"
        }
    
    async def initialize_payment(self, email: str, amount: float, reference: str, metadata: dict = None):
        url = f"{PAYSTACK_BASE_URL}/transaction/initialize"
        data = {
            "email": email,
            "amount": int(amount * 100),  # Paystack uses kobo (for NGN) or cents
            "reference": reference,
            "metadata": metadata or {}
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=data, headers=self.headers)
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Paystack error: {response.text}"
                )
            
            return response.json()
    
    async def verify_payment(self, reference: str):
        url = f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=self.headers)
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Paystack error: {response.text}"
                )
            
            return response.json()
    
    async def create_subscription(self, customer_email: str, plan_code: str, authorization_code: str):
        url = f"{PAYSTACK_BASE_URL}/subscription"
        data = {
            "customer": customer_email,
            "plan": plan_code,
            "authorization": authorization_code
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=data, headers=self.headers)
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Paystack error: {response.text}"
                )
            
            return response.json()
    
    async def disable_subscription(self, subscription_code: str, email_token: str):
        url = f"{PAYSTACK_BASE_URL}/subscription/disable"
        data = {
            "code": subscription_code,
            "token": email_token
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=data, headers=self.headers)
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Paystack error: {response.text}"
                )
            
            return response.json()

paystack_service = PaystackService()