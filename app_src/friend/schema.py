from datetime import datetime
from pydantic import BaseModel, EmailStr
from uuid import UUID
from db.models import FriendRequestStatus
        
class FriendRequestSchema(BaseModel):
    receiver_email: EmailStr
        
class FriendLinkSchema(BaseModel):
    user_id: UUID
    friend_id: UUID

    class Config:
        from_attributes = True
        
class FriendRequestResponseSchema(BaseModel):
    id: UUID
    sender_id: UUID
    sender_email: str
    status: FriendRequestStatus
    created_at: datetime

    class Config:
        from_attributes = True
        
class AcceptFriendRequestSchema(BaseModel):
    sender_email: EmailStr