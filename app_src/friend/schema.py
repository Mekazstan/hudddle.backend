from pydantic import BaseModel, EmailStr
from uuid import UUID
        
class FriendRequestSchema(BaseModel):
    receiver_email: EmailStr
        
class FriendLinkSchema(BaseModel):
    user_id: UUID
    friend_id: UUID

    class Config:
        from_attributes = True