from pydantic import BaseModel, Field
from typing import Optional
from db.models import TaskStatus
from datetime import datetime
from uuid import UUID

class WorkroomSchema(BaseModel):
    id: UUID
    created_at: datetime
    updated_at: datetime
    name: str
    description: Optional[str] = None
    created_by: UUID

    class Config:
        from_attributes = True
    
class WorkroomMemberLinkSchema(BaseModel):
    workroom_id: UUID
    user_id: UUID

    class Config:
        from_attributes = True
    
class WorkroomCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: Optional[str] = None

class WorkroomUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    description: Optional[str] = None


class WorkroomTaskCreate(BaseModel):
    title: str = Field(..., min_length=1, description="Title of the task")
    description: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    due_date: Optional[datetime] = None
    
class LeaderboardSchema(BaseModel):
    id: UUID
    created_at: datetime
    updated_at: datetime
    workroom_id: UUID
    user_id: UUID
    score: int
    teamwork_score: int
    rank: Optional[int] = None

    class Config:
        from_attributes = True