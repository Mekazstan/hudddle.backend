from pydantic import BaseModel, Field
from typing import Optional


class WorkroomCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: Optional[str] = None

class WorkroomUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    description: Optional[str] = None