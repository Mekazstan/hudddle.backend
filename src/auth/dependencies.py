from fastapi import Request, status, Depends
from fastapi.security import HTTPBearer
from fastapi.exceptions import HTTPException
from fastapi.security.http import HTTPAuthorizationCredentials
from .utils import decode_token
from sqlmodel.ext.asyncio.session import AsyncSession
from src.db.mongo import token_in_blocklist
from src.db.main import get_session
from .service import UserService
from typing import Any, List
from src.db.models import User


user_service = UserService()


class TokenBearer(HTTPBearer):
    def __init__(self, auto_error=True):
        super().__init__(auto_error=auto_error)
        
    async def __call__(self, request: Request) -> HTTPAuthorizationCredentials | None:
        creds = await super().__call__(request)
        token = creds.credentials
        token_data = decode_token(token)
        if not self.token_valid(token):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or Expired Token")
        if await token_in_blocklist(token_data["jti"]):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or Expired Token")
        self.verify_token_data(token_data)
        return token_data
    
    def token_valid(self, token: str) -> bool:
        token_data = decode_token(token)
        return token_data is not None
    
    def verify_token_data(self, token_data):
        raise NotImplementedError("Please Override this method in child classes")
            

class AccessTokenBearer(TokenBearer):
    def verify_token_data(self, token_data: dict) -> None:
        if token_data and token_data["refresh"]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Please provide a valid access token")

class RefreshTokenBearer(TokenBearer):
    def verify_token_data(self, token_data: dict) -> None:
        if token_data and not token_data["refresh"]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Please provide a valid refresh token")
            
async def get_current_user(
    token_details: dict = Depends(AccessTokenBearer()),
    session: AsyncSession = Depends(get_session),
):
    print(token_details)
    user_email = token_details["user"]["email"]

    user = await user_service.get_user_by_email(user_email, session)

    return user


class RoleChecker:
    def __init__(self, allowed_roles: List[str]) -> None:
        self.allowed_roles = allowed_roles
        
    def __call__(self, current_user: User = Depends(get_current_user)) -> Any:
        if not current_user.is_verified:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account Not verified")
        if current_user.role in self.allowed_roles:
            return True
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, 
                            detail="You do not have enough permissions to perform this action")