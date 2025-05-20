from pydantic import BaseModel, Field, validator
from typing import Optional, List
from datetime import datetime, timezone
from uuid import UUID
from db.models import TaskStatus
from workroom.schema import WorkroomPerformanceMetricSchema

class TaskSchema(BaseModel):
    id: UUID
    created_at: datetime
    updated_at: datetime
    title: str
    duration: Optional[str] = None
    is_recurring: bool
    status: TaskStatus
    category: Optional[str] = None
    task_tools: Optional[List[str]] = None
    deadline: Optional[datetime] = None
    due_by: Optional[datetime] = None
    task_point: int                  
    completed_at: Optional[datetime] = None
    created_by_id: UUID
    workroom_id: Optional[UUID] = None
    assigned_user_ids: Optional[List[UUID]] = []

    class Config:
        from_attributes = True

class TaskCollaboratorSchema(BaseModel):
    task_id: UUID
    user_id: UUID
    invited_by_id: UUID

    class Config:
        from_attributes = True


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, description="Title of the task")
    duration: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    is_recurring: bool = False
    deadline: Optional[datetime] = None
    due_by: Optional[datetime] = None
    task_point: int = 10            
    workroom_id: Optional[UUID] = None
    category: Optional[str] = None
    task_tools: Optional[List[str]] = None
    assigned_user_ids: Optional[List[UUID]] = []
    
    @validator('deadline', 'due_by', pre=True)
    def parse_datetime(cls, v):
        if v is None:
            return None
            
        if isinstance(v, str):
            try:
                # Parse string to datetime
                dt = datetime.fromisoformat(v)
                # Ensure timezone is set
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                raise ValueError("Invalid datetime format. Use ISO format (e.g., '2025-05-12T12:00:00+00:00')")
                
        elif isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v
            
        raise ValueError("Expected datetime or ISO format string")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }
    
class TaskUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, description="Title of the task")
    duration: Optional[str] = None
    status: Optional[TaskStatus] = None
    is_recurring: Optional[bool] = None
    deadline: Optional[datetime] = None
    due_by: Optional[datetime] = None
    task_point: Optional[int] = Field(None, ge=1, description="Points must be positive")
    workroom_id: Optional[UUID] = None
    category: Optional[str] = None
    task_tools: Optional[List[str]] = None
    assigned_user_ids: Optional[List[UUID]] = None

    @validator('deadline', 'due_by', pre=True)
    def parse_datetime(cls, v):
        if v is None:
            return None
            
        if isinstance(v, str):
            try:
                # Parse string to datetime
                dt = datetime.fromisoformat(v)
                # Ensure timezone is set
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                raise ValueError("Invalid datetime format. Use ISO format (e.g., '2025-05-12T12:00:00+00:00')")
                
        elif isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v
            
        raise ValueError("Expected datetime or ISO format string")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }
    
class MemberMetricSchema(BaseModel):
    kpi_name: str
    metric_value: int
    weight: int

class FullMemberSchema(BaseModel):
    id: UUID
    name: str
    email: str
    avatar_url: Optional[str]
    xp: int
    level: int
    productivity: float
    average_task_time: float
    daily_active_minutes: int
    teamwork_collaborations: int
    metrics: List[MemberMetricSchema]

class WorkroomDetailsSchema(BaseModel):
    id: UUID
    name: str
    kpis: Optional[str]
    members: List[FullMemberSchema]
    completed_task_count: int
    pending_task_count: int
    tasks: List[TaskSchema]
    performance_metrics: List[WorkroomPerformanceMetricSchema]
