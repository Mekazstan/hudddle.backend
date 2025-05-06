from sqlalchemy import (Column, Integer, String, Numeric, ForeignKey, 
                        DateTime, Enum, ARRAY, Boolean, Date)
from sqlalchemy.orm import relationship, declarative_base
import sqlalchemy.dialects.postgresql as pg
from datetime import datetime, date, time
from enum import Enum as PyEnum
from uuid import uuid4

Base = declarative_base()

def create_datetime_column():
    return DateTime(timezone=False)

class TaskStatus(str, PyEnum):
    PENDING = "PENDING"
    OVERDUE = "OVERDUE"
    COMPLETED = "COMPLETED"
    
class FriendRequestStatus(str, PyEnum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"
    
class LevelCategory(str, PyEnum):
    LEADER = "Leader"
    WORKAHOLIC = "Workaholic"
    TEAM_PLAYER = "Team Player"
    SLACKER = "Slacker"

class LevelTier(str, PyEnum):
    BEGINNER = "Beginner"
    INTERMEDIATE = "Intermediate"
    ADVANCED = "Advanced"
    EXPERT = "Expert"

class UserLevel(Base):
    __tablename__ = "user_levels"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)
    level_category = Column(Enum(LevelCategory))
    level_tier = Column(Enum(LevelTier))
    level_points = Column(Integer, default=0)

    user = relationship("User", back_populates="levels")
    
class FriendLink(Base):
    __tablename__ = "friend_links"

    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), primary_key=True)
    friend_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), primary_key=True)
       
class WorkroomMemberLink(Base):
    __tablename__ = "workroom_member_links"
    
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete='CASCADE'), primary_key=True)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), primary_key=True)
    
class TaskCollaborator(Base):
    __tablename__ = "task_collaborators"

    task_id = Column(pg.UUID(as_uuid=True), ForeignKey("tasks.id", ondelete='CASCADE'), primary_key=True)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), primary_key=True)
    invited_by_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'))

    task = relationship("Task", back_populates="collaborators")
    invited_by = relationship(
        "User", 
        back_populates="task_collaborations_invited",
        foreign_keys=[invited_by_id]
    )
    user = relationship(
        "User", 
        back_populates="task_collaborations_user",
        foreign_keys=[user_id]
    )

class PasswordResetOTP(Base):
    __tablename__ = "password_reset_otps"
    
    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False, index=True)
    otp = Column(String(4), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class User(Base):
    __tablename__ = "users"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    auth_provider = Column(String, nullable=True)
    username = Column(String, index=True, nullable=True)
    email = Column(String, unique=True, index=True, nullable=False)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user", nullable=False)
    xp = Column(Integer, default=0, nullable=False)
    level = Column(Integer, default=1, nullable=False)
    avatar_url = Column(String, nullable=True)
    is_verified = Column(Boolean, default=False, nullable=False)
    productivity = Column(Numeric, default=0.0, nullable=False)
    average_task_time = Column(Numeric, default=0.0, nullable=False)
    daily_active_minutes = Column(Integer, default=0)
    last_activity_start = Column(DateTime, nullable=True)
    teamwork_collaborations = Column(Integer, default=0)
    daily_teamwork_collaborations = Column(Integer, default=0)

    user_type = Column(String, nullable=True)
    find_us = Column(String, nullable=True)
    software_used = Column(ARRAY(String), nullable=True)

    workrooms = relationship(
        "Workroom", 
        secondary="workroom_member_links", 
        back_populates="members"
    )
    levels = relationship("UserLevel", back_populates="user", cascade="all, delete-orphan")
    task_collaborations_invited = relationship(
        "TaskCollaborator", 
        back_populates="invited_by",
        foreign_keys="[TaskCollaborator.invited_by_id]",
        cascade="all, delete-orphan"
    )
    task_collaborations_user = relationship(
        "TaskCollaborator", 
        back_populates="user",
        foreign_keys="[TaskCollaborator.user_id]",
        cascade="all, delete-orphan"
    )
    streak = relationship("UserStreak", back_populates="user", uselist=False)
    created_tasks = relationship("Task", back_populates="created_by", cascade="all, delete-orphan")
    leaderboards = relationship("Leaderboard", back_populates="user", cascade="all, delete-orphan")
    friends = relationship(
        "User", 
        secondary="friend_links", 
        primaryjoin="User.id==FriendLink.user_id",
        secondaryjoin="User.id==FriendLink.friend_id",
    )
    
class FriendRequest(Base):
    __tablename__ = "friend_requests"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    sender_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)
    receiver_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)
    status = Column(Enum(FriendRequestStatus), default=FriendRequestStatus.pending, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
class WorkroomLiveSession(Base):
    __tablename__ = "workroom_live_sessions"
    
    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete='CASCADE'), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    screen_sharer_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='SET NULL'), nullable=True)
    is_active = Column(Boolean, default=True)
    
    workroom = relationship("Workroom", back_populates="live_sessions")
    screen_sharer = relationship("User")

class Workroom(Base):
    __tablename__ = "workrooms"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    name = Column(String, index=True, nullable=False)
    description = Column(String, nullable=True)
    created_by = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)

    members = relationship(
        "User", 
        secondary="workroom_member_links", 
        back_populates="workrooms"
    )
    tasks = relationship("Task", back_populates="workroom", cascade="all, delete-orphan")
    leaderboards = relationship("Leaderboard", back_populates="workroom", cascade="all, delete-orphan")
    live_sessions = relationship("WorkroomLiveSession", back_populates="workroom", cascade="all, delete-orphan")

class Task(Base):
    __tablename__ = "tasks"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    is_recurring = Column(Boolean, default=False, nullable=False)
    title = Column(String, index=True, nullable=False)
    duration = Column(String, nullable=True)
    category = Column(String, nullable=True)
    task_tools = Column(pg.ARRAY(String), nullable=True)
    deadline = Column(DateTime, default=datetime.combine(date.today(), time(23, 59, 59)), nullable=True)
    due_by = Column(DateTime, default=datetime.combine(date.today(), datetime.now().time().replace(microsecond=0)), nullable=True)
    task_point = Column(Integer, default=10, nullable=False)
    status = Column(Enum(TaskStatus), default=TaskStatus.PENDING, nullable=False)
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete='SET NULL'), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_by_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)

    collaborators = relationship("TaskCollaborator", back_populates="task", cascade="all, delete-orphan")
    created_by = relationship("User", back_populates="created_tasks")
    workroom = relationship("Workroom", back_populates="tasks")

class Achievement(Base):
    __tablename__ = "achievements"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    name = Column(String, index=True, nullable=False)
    description = Column(String, nullable=True)
    xp_reward = Column(Integer, default=0, nullable=False)

class Leaderboard(Base):
    __tablename__ = "leaderboards"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete='CASCADE'), nullable=False)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)
    score = Column(Integer, default=0, nullable=False)
    teamwork_score = Column(Integer, default=0, nullable=False)
    rank = Column(Integer, nullable=True)

    workroom = relationship("Workroom", back_populates="leaderboards")
    user = relationship("User", back_populates="leaderboards")
       
class UserStreak(Base):
    __tablename__ = "user_streaks"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)
    current_streak = Column(Integer, default=1)
    last_active_date = Column(Date, nullable=True)
    highest_streak = Column(Integer, default=1)

    user = relationship("User", back_populates="streak")

