from sqlalchemy import (JSON, Column, Float, Integer, String, Numeric, ForeignKey, Table, Text,
                        DateTime, Enum, ARRAY, Boolean, Date, UniqueConstraint)
from sqlalchemy.orm import relationship, declarative_base
import sqlalchemy.dialects.postgresql as pg
from datetime import datetime, date
from enum import Enum as PyEnum
from uuid import uuid4

Base = declarative_base()

def create_datetime_column():
    return DateTime(timezone=False)

task_assignees = Table(
    "task_assignees",
    Base.metadata,
    Column("task_id", pg.UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)

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
    
class ExternalServiceType(str, PyEnum):
    ASANA = "asana"
    TRELLO = "trello"
    JIRA = "jira"
    NOTION = "notion"
    SLACK = "slack"
    ZOOM = "zoom"
    TOGGL = "toggl"
    FIGMA = "figma"
    MIRO = "miro"
    GOOGLE_DOCS = "google_docs"
    
class SubscriptionPlan(str, PyEnum):
    BASIC = "basic"
    PRO = "pro"
    ENTERPRISE = "enterprise"

class PaymentStatus(str, PyEnum):
    PENDING = "pending"
    SUCCESSFUL = "successful"
    FAILED = "failed"

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
    
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete="CASCADE"), primary_key=True, nullable=False)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, nullable=False)
    joined_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    workroom = relationship(
        "Workroom",
        back_populates="member_links",
        overlaps="members"
    )
    user = relationship(
        "User",
        back_populates="workroom_links",
        overlaps="workrooms"
    )
    
class TaskCollaborator(Base):
    __tablename__ = "task_collaborators"

    task_id = Column(pg.UUID(as_uuid=True), ForeignKey("tasks.id", ondelete='CASCADE'), primary_key=True)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), primary_key=True)
    invited_by_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='SET NULL'))

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
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class User(Base):
    __tablename__ = "users"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    auth_provider = Column(String, nullable=True)
    username = Column(String, index=True, nullable=True)
    email = Column(String, unique=True, index=True, nullable=False)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    password_hash = Column(String, nullable=True)
    role = Column(String, default="user", nullable=False)
    xp = Column(Integer, default=0, nullable=False)
    level = Column(Integer, default=1, nullable=False)
    avatar_url = Column(String, nullable=True)
    is_verified = Column(Boolean, default=False, nullable=False)
    productivity = Column(Numeric, default=0.0, nullable=False)
    average_task_time = Column(Numeric, default=0.0, nullable=False)
    daily_active_minutes = Column(Integer, default=0)
    last_activity_start = Column(DateTime(timezone=True), nullable=True)
    teamwork_collaborations = Column(Integer, default=0)
    daily_teamwork_collaborations = Column(Integer, default=0)
    is_user_onboarded = Column(Boolean, default=False, nullable=False)

    user_type = Column(String, nullable=True)
    find_us = Column(String, nullable=True)
    software_used = Column(ARRAY(String), nullable=True)

    external_connections = relationship(
        "ExternalServiceConnection", 
        back_populates="user",
        cascade="all, delete-orphan"
    )
    payments = relationship("Payment", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")
    workrooms_created = relationship("Workroom", back_populates="created_by_user")
    workrooms = relationship(
        "Workroom",
        secondary="workroom_member_links",
        back_populates="members",
        overlaps="workroom_links",
        viewonly=True
    )
    workroom_links = relationship(
        "WorkroomMemberLink",
        back_populates="user",
        cascade="all, delete-orphan",
        overlaps="workrooms"
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
    kpi_summaries = relationship(
        "UserKPISummary",
        back_populates="user",
        cascade="all, delete-orphan"
    )
    assigned_tasks = relationship("Task", secondary=task_assignees, back_populates="assigned_users")
    streak = relationship("UserStreak", back_populates="user", uselist=False)
    created_tasks = relationship("Task", back_populates="created_by", cascade="all, delete-orphan")
    leaderboards = relationship("Leaderboard", back_populates="user", cascade="all, delete-orphan")
    friends = relationship(
        "User", 
        secondary="friend_links", 
        primaryjoin="User.id==FriendLink.user_id",
        secondaryjoin="User.id==FriendLink.friend_id",
    )
    sent_friend_requests = relationship(
        "FriendRequest",
        foreign_keys="[FriendRequest.sender_id]",
        back_populates="sender",
        cascade="all, delete-orphan"
    )

    received_friend_requests = relationship(
        "FriendRequest",
        foreign_keys="[FriendRequest.receiver_id]",
        back_populates="receiver",
        cascade="all, delete-orphan"
    )

class Payment(Base):
    __tablename__ = "payments"
    
    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)
    amount = Column(Numeric, nullable=False)
    currency = Column(String, default="NGN", nullable=False)
    reference = Column(String, unique=True, nullable=False)
    status = Column(Enum(PaymentStatus), default=PaymentStatus.PENDING, nullable=False)
    payment_method = Column(String, nullable=True)
    paystack_transaction_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = relationship("User", back_populates="payments")
    
class Subscription(Base):
    __tablename__ = "subscriptions"
    
    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)
    plan = Column(Enum(SubscriptionPlan), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    start_date = Column(DateTime(timezone=True), default=datetime.utcnow)
    end_date = Column(DateTime(timezone=True), nullable=True)
    paystack_subscription_id = Column(String, nullable=True)
    paystack_customer_code = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = relationship("User", back_populates="subscriptions")
    
class WorkroomPerformanceMetric(Base):
    __tablename__ = "workroom_performance_metrics"
    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete='CASCADE'), nullable=False)
    kpi_name = Column(String, nullable=True)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)
    weight = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint('workroom_id', 'user_id', 'kpi_name', name='_workroom_performance_metric_uc'),
    )
    workroom = relationship("Workroom", back_populates="performance_metrics")
    user = relationship("User")

class FriendRequest(Base):
    __tablename__ = "friend_requests"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    sender_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)
    receiver_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)
    status = Column(Enum(FriendRequestStatus), default=FriendRequestStatus.pending, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    
    sender = relationship("User", foreign_keys=[sender_id], back_populates="sent_friend_requests")
    receiver = relationship("User", foreign_keys=[receiver_id], back_populates="received_friend_requests")
    
class WorkroomLiveSession(Base):
    __tablename__ = "workroom_live_sessions"
    
    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete='CASCADE'), nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    screen_sharer_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='SET NULL'), nullable=True)
    is_active = Column(Boolean, default=True)
    start_time = Column(DateTime, default=datetime.utcnow)
    
    workroom = relationship("Workroom", back_populates="live_sessions")
    screen_sharer = relationship("User")

class Workroom(Base):
    __tablename__ = "workrooms"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    name = Column(String, index=True, nullable=False)
    created_by = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)

    members = relationship(
        "User",
        secondary="workroom_member_links",
        back_populates="workrooms",
        overlaps="member_links",
        viewonly=True
    )
    member_links = relationship(
        "WorkroomMemberLink",
        back_populates="workroom",
        cascade="all, delete-orphan",
        overlaps="members"
    )
    tasks = relationship("Task", back_populates="workroom", cascade="all, delete-orphan")
    created_by_user = relationship("User", back_populates="workrooms_created")
    leaderboards = relationship("Leaderboard", back_populates="workroom", cascade="all, delete-orphan")
    live_sessions = relationship("WorkroomLiveSession", back_populates="workroom", cascade="all, delete-orphan")
    kpi_metric_history = relationship("WorkroomKPIMetricHistory", back_populates="workroom", cascade="all, delete-orphan")
    kpi_summary = relationship("WorkroomKPISummary", back_populates="workroom", cascade="all, delete-orphan")
    kpi_overall = relationship("WorkroomOverallKPI", back_populates="workroom", cascade="all, delete-orphan")
    performance_metrics = relationship("WorkroomPerformanceMetric", back_populates="workroom", cascade="all, delete-orphan")
    
class Task(Base):
    __tablename__ = "tasks"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    is_recurring = Column(Boolean, default=False, nullable=False)
    title = Column(String, index=True, nullable=False)
    duration = Column(String, nullable=True)
    category = Column(String, nullable=True)
    task_tools = Column(pg.ARRAY(String), nullable=True)
    deadline = Column(DateTime(timezone=True), nullable=True)
    due_by = Column(DateTime(timezone=True), nullable=True)
    kpi_link = Column(String, nullable=True)
    task_point = Column(Integer, default=10, nullable=False)
    status = Column(Enum(TaskStatus), default=TaskStatus.PENDING, nullable=False)
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete='SET NULL'), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_by_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)

    collaborators = relationship("TaskCollaborator", back_populates="task", cascade="all, delete-orphan")
    created_by = relationship("User", back_populates="created_tasks")
    workroom = relationship("Workroom", back_populates="tasks")
    assigned_users = relationship("User", secondary=task_assignees, back_populates="assigned_tasks")

class Achievement(Base):
    __tablename__ = "achievements"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    name = Column(String, index=True, nullable=False)
    description = Column(String, nullable=True)
    xp_reward = Column(Integer, default=0, nullable=False)

class Leaderboard(Base):
    __tablename__ = "leaderboards"

    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete='CASCADE'), nullable=False)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete='CASCADE'), nullable=False)

    score = Column(Integer, default=0, nullable=False)
    teamwork_score = Column(Integer, default=0, nullable=True)
    rank = Column(Integer, nullable=True)
    kpi_score = Column(Float, default=0.0, nullable=False)
    task_score = Column(Integer, default=0, nullable=False)
    engagement_score = Column(Integer, default=0, nullable=False)

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

class WorkroomOverallKPI(Base):
    __tablename__ = "workroom_overall_kpis"
    id = Column(pg.UUID(as_uuid=True), default=uuid4, primary_key=True)
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete='CASCADE'), nullable=False)
    date = Column(Date, default=date.today)
    overall_alignment_score = Column(Numeric, nullable=False)

    workroom = relationship("Workroom")

    __table_args__ = (
        UniqueConstraint('workroom_id', 'date', name='_workroom_overall_kpi_uc'),
    )
    
class UserKPISummary(Base):
    __tablename__ = "user_kpi_summary"

    id = Column(pg.UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    session_id = Column(pg.UUID(as_uuid=True), ForeignKey("workroom_live_sessions.id", ondelete="SET NULL"))
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete="CASCADE"))
    date = Column(Date, default=datetime.utcnow().date)
    overall_alignment_percentage = Column(Float)
    kpi_breakdown = Column(JSON)
    summary_text = Column(Text)

    user = relationship("User", back_populates="kpi_summaries")
    
class UserKPIMetricHistory(Base):
    __tablename__ = "user_kpi_metric_history"

    id = Column(pg.UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete="CASCADE"))
    kpi_name = Column(String)
    date = Column(Date)
    alignment_percentage = Column(Float)

class WorkroomKPISummary(Base):
    __tablename__ = "workroom_kpi_summary"

    id = Column(pg.UUID(as_uuid=True), primary_key=True, default=uuid4)
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete="CASCADE"))
    date = Column(Date, default=datetime.utcnow().date)
    overall_alignment_percentage = Column(Float)
    kpi_breakdown = Column(JSON)
    summary_text = Column(Text)

    workroom = relationship("Workroom", back_populates="kpi_summary")
    
class WorkroomKPIMetricHistory(Base):
    __tablename__ = "workroom_kpi_metric_history"

    id = Column(pg.UUID(as_uuid=True), primary_key=True, default=uuid4)
    workroom_id = Column(pg.UUID(as_uuid=True), ForeignKey("workrooms.id", ondelete="CASCADE"))
    date = Column(Date, default=datetime.utcnow().date)
    kpi_name = Column(String, nullable=False)
    metric_value = Column(Float, nullable=False)

    workroom = relationship("Workroom", back_populates="kpi_metric_history")

class ExternalServiceConnection(Base):
    __tablename__ = "external_service_connections"
    
    id = Column(pg.UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(pg.UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    service_type = Column(Enum(ExternalServiceType))
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    connection_metadata = Column(JSON, nullable=True)
    
    user = relationship("User", back_populates="external_connections")
    
class ImportedTask(Base):
    __tablename__ = "imported_tasks"
    
    id = Column(pg.UUID(as_uuid=True), primary_key=True, default=uuid4)
    external_id = Column(String, nullable=False)  # Original ID from external service
    source_service = Column(Enum(ExternalServiceType))
    task_id = Column(pg.UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"))
    last_sync = Column(DateTime(timezone=True))
    raw_data = Column(JSON)  # Original payload
    
    task = relationship("Task")
    
# [Asana, Trello, Jira, Notion, Google Docs, Slack, Zoom, Toggl Track, RescueTime, Figma, Miro]