from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from uuid import UUID
from src.db.models import TaskStatus


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, description="Title of the task")
    description: Optional[str] = None
    status: TaskStatus = TaskStatus.TODO
    due_date: Optional[datetime] = None
    workroom_id: Optional[UUID] = None
    
class TaskUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, description="Title of the task")  # Optional, at least 1 char if provided
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    due_date: Optional[datetime] = None
    workroom_id: Optional[UUID] = None