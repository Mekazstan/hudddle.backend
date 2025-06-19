from pydantic import BaseModel, ConfigDict, Field, EmailStr, field_validator
from typing import Union, Dict, List, Any, Optional
from app_src.db.models import TaskStatus
from datetime import datetime
from uuid import UUID
from datetime import date
from app_src.auth.schema import UserSchema
from app_src.tasks.schema import TaskSchema

class WorkroomPerformanceMetricSchema(BaseModel):
    kpi_name: str = Field(..., min_length=2, max_length=50)
    weight: int = Field(..., gt=0, le=10, description="Importance weight 1-10")
    
    model_config = ConfigDict(from_attributes=True)
        
class WorkroomSchema(BaseModel):
    id: UUID
    name: str
    created_by: UUID
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
    performance_metrics: List[WorkroomPerformanceMetricSchema] = []
    friend_emails: List[EmailStr] = []
    

class WorkroomUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
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
    kpi_breakdown: List[KPIBreakdown]

class WorkroomKPIMetricHistorySchema(BaseModel):
    kpi_name: str
    date: date
    alignment_percentage: float
    
class MemberMetricSchema(BaseModel):
    kpi_name: str
    percentage: float
    weight: Optional[float] = None
    metric_value: Optional[float] = None

class WorkroomKPISummarySchema(BaseModel):
    overall_alignment_percentage: float
    summary_text: Optional[str] = None
    kpi_breakdown: Union[List[MemberMetricSchema], Dict[str, float]]  # Accepts both formats

    @field_validator('kpi_breakdown', mode='before')
    def validate_kpi_breakdown(cls, v):
        if isinstance(v, dict):
            # Convert dict to list of MemberMetricSchema if needed
            return [{"kpi_name": k, "percentage": v} for k, v in v.items()]
        return v
    
class KPIBreakdownType(BaseModel):
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        # Accept either a list of MemberMetricSchema or a dictionary
        return {
            'type': 'union',
            'choices': [
                {'type': 'list', 'items': handler(MemberMetricSchema)},
                {'type': 'dict', 'keys': {'type': 'str'}, 'values': {'type': 'float'}}
            ]
        }


class UserKPIMetricHistorySchema(BaseModel):
    kpi_name: str
    date: date
    alignment_percentage: float
    
class UserKPISummarySchema(BaseModel):
    overall_alignment_percentage: float
    summary_text: Optional[str] = None
    kpi_breakdown: Union[List[MemberMetricSchema], Dict[str, float]]  # Accepts both formats

    @field_validator('kpi_breakdown', mode='before')
    def validate_kpi_breakdown(cls, v):
        if isinstance(v, dict):
            # Convert dict to list of MemberMetricSchema if needed
            return [{"kpi_name": k, "percentage": v} for k, v in v.items()]
        return v
    
class LeaderboardEntrySchema(BaseModel):
    score: float
    rank: int
    kpi_score: float
    task_score: float
    teamwork_score: Optional[float] = None
    engagement_score: Optional[float] = None

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
    kpi_summary: Optional[UserKPISummarySchema] = None
    kpi_metric_history: List[UserKPIMetricHistorySchema] = []
    leaderboard_entry: Optional[LeaderboardEntrySchema] = None

class WorkroomDetailsSchema(BaseModel):
    id: UUID
    name: str
    members: List[FullMemberSchema]
    completed_task_count: int
    pending_task_count: int
    tasks: List[TaskSchema]
    performance_metrics: List[WorkroomPerformanceMetricSchema]
    workroom_kpi_summary: Optional[WorkroomKPISummarySchema] = None
    workroom_kpi_metric_history: List[WorkroomKPIMetricHistorySchema] = []
