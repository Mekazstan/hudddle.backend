from sqlmodel import SQLModel, Field, Column, Relationship, String
from typing import List, Optional
from datetime import datetime
from enum import Enum
from sqlalchemy import DateTime
from uuid import UUID, uuid4
import sqlalchemy.dialects.postgresql as pg

def create_datetime_column():
    return DateTime(timezone=False)

class TaskStatus(str, Enum):
    TODO = "TODO"
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"

class User(SQLModel, table=True):
    __tablename__ = "users"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())

    firebase_uid: Optional[str] = Field(default=None, index=True)
    username: Optional[str] = Field(index=True, nullable=True)
    email: str = Field(unique=True, index=True, nullable=False)
    first_name: Optional[str] = Field(default=None)
    last_name: Optional[str] = Field(default=None)
    password_hash: str = Field(nullable=False)
    role: str = Field(default="member", nullable=False)
    xp: int = Field(default=0, nullable=False)
    level: int = Field(default=1, nullable=False)
    badges: List[str] = Field(default_factory=list, sa_column=Column(pg.ARRAY(String)))
    avatar_url: Optional[str] = Field(default=None)
    is_verified: bool = Field(default=False, nullable=False)
    productivity: float = Field(default=0.0, nullable=False)
    average_task_time: float = Field(default=0.0, nullable=False)

    user_type: Optional[str] = Field(default=None)
    find_us: Optional[str] = Field(default=None)
    software_used: Optional[str] = Field(default=None)

    workrooms: List["Workroom"] = Relationship(back_populates="members")
    created_tasks: List["Task"] = Relationship(back_populates="created_by")
    leaderboards: List["Leaderboard"] = Relationship(back_populates="user")

class Workroom(SQLModel, table=True):
    __tablename__ = "workrooms"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())

    name: str = Field(index=True, nullable=False)
    description: Optional[str] = Field(default=None)
    created_by: UUID = Field(foreign_key="users.id", nullable=False)

    members: List[User] = Relationship(back_populates="workrooms")
    tasks: List["Task"] = Relationship(back_populates="workroom")
    leaderboards: List["Leaderboard"] = Relationship(back_populates="workroom")

class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())

    title: str = Field(index=True, nullable=False)
    description: Optional[str] = Field(default=None)
    status: TaskStatus = Field(default=TaskStatus.TODO, nullable=False)
    due_date: Optional[datetime] = Field(default=None, description="Due date in UTC")
    created_by_id: UUID = Field(foreign_key="users.id", nullable=False)
    workroom_id: Optional[UUID] = Field(foreign_key="workrooms.id", default=None)
    created_by: "User" = Relationship(back_populates="created_tasks")
    workroom: Optional["Workroom"] = Relationship(back_populates="tasks")

class Achievement(SQLModel, table=True):
    __tablename__ = "achievements"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())

    name: str = Field(index=True, nullable=False)
    description: Optional[str] = Field(default=None)
    xp_reward: int = Field(default=0, nullable=False)
    badge_url: Optional[str] = Field(default=None)

class Leaderboard(SQLModel, table=True):
    __tablename__ = "leaderboards"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())

    workroom_id: UUID = Field(foreign_key="workrooms.id", nullable=False)
    user_id: UUID = Field(foreign_key="users.id", nullable=False)
    score: int = Field(default=0, nullable=False)
    rank: Optional[int] = Field(default=None)

    workroom: Workroom = Relationship(back_populates="leaderboards")
    user: User = Relationship(back_populates="leaderboards")

class DailyChallenge(SQLModel, table=True):
    __tablename__ = "daily_challenges"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())

    description: str = Field(index=True, nullable=False)
    points: int = Field(default=0, nullable=False)

class Team(SQLModel, table=True):
    __tablename__ = "teams"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column=create_datetime_column())

    name: str = Field(index=True, nullable=False)
    drop_ins: int = Field(default=0, nullable=False)