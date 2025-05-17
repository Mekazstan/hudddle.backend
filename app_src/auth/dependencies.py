from fastapi import status, Depends
from fastapi.security import HTTPBearer
from fastapi.exceptions import HTTPException
from fastapi.security.http import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from db.db_connect import get_session
from db.models import User
from .utils import decode_access_token
from .service import UserService
from typing import Any, List
from .schema import UserSchema


user_service = UserService()

class AccessTokenBearer:
    def __init__(self, required_purpose: str = None):
        self.scheme = HTTPBearer(auto_error=False)
        self.required_purpose = required_purpose
        
    scheme = HTTPBearer(auto_error=False)
    
    async def __call__(self, credentials: HTTPAuthorizationCredentials = Depends(scheme)) -> dict:
        if not credentials:
            raise HTTPException(status_code=401, detail="Missing token")
        
        token_data = decode_access_token(credentials.credentials)
        if not token_data:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        if self.required_purpose and token_data.get("purpose") != self.required_purpose:
            raise HTTPException(status_code=403, detail="Invalid token purpose")
        
        return token_data
    
async def get_current_user_model(
    token_data: dict = Depends(AccessTokenBearer()),
    session: AsyncSession = Depends(get_session),
) -> User:
    user_id = token_data.get("user", {}).get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user in token")
    
    user = await user_service.get_user_by_id(user_id, session)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return user

async def get_current_user(
    token_data: dict = Depends(AccessTokenBearer()),
    session: AsyncSession = Depends(get_session),
) -> UserSchema:
    user = await get_current_user_model(token_data, session)
    return UserSchema.from_orm(user)

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
        