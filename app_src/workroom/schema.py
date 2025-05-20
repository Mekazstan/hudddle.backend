from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional
from db.models import TaskStatus
from datetime import datetime
from uuid import UUID
from auth.schema import UserSchema

class WorkroomMetricSchema(BaseModel):
    metric_name: str
    metric_value: int
    
    class Config:
        from_attributes = True

class WorkroomPerformanceMetricSchema(BaseModel):
    kpi_name: str
    metric_value: int = Field(default=1)
    weight: int = Field(..., gt=0, le=10)
        
class WorkroomSchema(BaseModel):
    id: UUID
    name: str
    created_by: UUID
    kpis: str
    metrics: Optional[List[WorkroomMetricSchema]] = None
    performance_metrics: List[WorkroomPerformanceMetricSchema] = None
    members: List[UserSchema]
    
    class Config:
        from_attributes = True
        arbitrary_types_allowed = True
        
    
class WorkroomMemberLinkSchema(BaseModel):
    workroom_id: UUID
    user_id: UUID
    joined_at: datetime

    class Config:
        from_attributes = True
    
class WorkroomCreate(BaseModel):
    name: str = Field(..., min_length=1)
    kpis: str = None
    performance_metrics: List[WorkroomPerformanceMetricSchema] = []
    friend_emails: List[EmailStr] = []
    

class WorkroomUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    kpis: Optional[str] = None
    performance_metrics: Optional[List[WorkroomPerformanceMetricSchema]] = []
    

class WorkroomTaskCreate(BaseModel):
    title: str = Field(..., min_length=1, description="Title of the task")
    status: TaskStatus = TaskStatus.PENDING
    due_by: Optional[datetime] = None
    
class LeaderboardSchema(BaseModel):
    id: UUID
    created_at: datetime
    updated_at: datetime
    workroom_id: UUID
    user_id: UUID
    score: int
    teamwork_score: int
    rank: Optional[int] = None
    kpi_score: float
    task_score: int
    engagement_score: int

    class Config:
        from_attributes = True
        
class ScreenshotData(BaseModel):
    user_id: UUID
    session_id: UUID
    image_data_url: str
    
class AnalyzedActivity(BaseModel):
    kpi_name: str = Field(..., description="The name of the Key Performance Indicator the activity relates to.")
    activity_description: str = Field(..., description="A brief description of the identified activity.")
    confidence_score: float = Field(..., description="A score indicating the LLM's confidence in the activity's relevance to the KPI (0.0 to 1.0).")

class ImageAnalysisResult(BaseModel):
    activities: List[AnalyzedActivity] = Field(..., description="A list of activities identified and categorized based on KPIs.")
    general_observations: str = Field(..., description="Any general observations from the screenshot that might not directly relate to a specific KPI.")
    
class KPIBreakdown(BaseModel):
    kpi_name: str
    percentage: float

class UserDailyKPIReport(BaseModel):
    summary_text: str
    overall_alignment_percentage: float
    kpi_breakdown: List[KPIBreakdown]
