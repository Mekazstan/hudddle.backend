from pydantic import BaseModel
from datetime import datetime
from uuid import UUID
from db.models import FriendRequestStatus
        
class FriendRequestSchema(BaseModel):
    id: UUID
    sender_id: UUID
    receiver_id: UUID
    status: FriendRequestStatus
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        
class FriendLinkSchema(BaseModel):
    user_id: UUID
    friend_id: UUID

    class Config:
        from_attributes = True