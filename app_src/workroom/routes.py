import logging
from fastapi import (Body, APIRouter, File, HTTPException, 
                     Depends, UploadFile, status)
from sqlalchemy import func, select, delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app_src.achievements.service import update_user_level
from app_src.db.db_connect import get_session
from .service import upload_audio_to_s3
from .schema import (WorkroomCreate, WorkroomPerformanceMetricSchema, 
                     WorkroomSchema, WorkroomTaskCreate, WorkroomUpdate)
from typing import List, Dict, Optional
from uuid import UUID
from app_src.db.models import (LevelCategory, UserKPISummary, Workroom, User, 
                               Task, TaskStatus, WorkroomLiveSession, 
                       WorkroomMemberLink, WorkroomPerformanceMetric)
from app_src.auth.dependencies import get_current_user
from app_src.tasks.schema import (FullMemberSchema, MemberMetricSchema, 
                          TaskSchema, WorkroomDetailsSchema)
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError
from app_src.config import Config
from app_src.celery_task import (
    process_image_and_store_task,
    process_audio_and_store_report_task,
    process_workroom_end_session,
    send_workroom_invites
)

workroom_router = APIRouter()

# AWS S3 Configuration
AWS_ACCESS_KEY_ID = Config.AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY = Config.AWS_SECRET_ACCESS_KEY
AWS_STORAGE_BUCKET_NAME = Config.AWS_STORAGE_BUCKET_NAME
AWS_REGION = Config.AWS_REGION

s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION,
)

async def _fetch_workroom_display_data(session: AsyncSession, user_id: UUID):
    """Helper function to fetch and format workroom display data with member avatars."""
    result = await session.execute(
        select(Workroom)
        .options(selectinload(Workroom.members), selectinload(Workroom.created_by_user))
        .where(
            Workroom.id.in_(
                select(WorkroomMemberLink.workroom_id).where(WorkroomMemberLink.user_id == user_id)
            )
        )
    )
    workrooms = result.scalars().all()
    display_data = []

    for wr in workrooms:
        members_display = [
            {
                "name": format_member_name(member.first_name, member.last_name),
                "avatar_url": getattr(member, "avatar_url", None)
            }
            for member in wr.members[:3]
        ]
        display_data.append({
            "id": wr.id,
            "title": wr.name,
            "created_by": format_member_name(wr.created_by_user.first_name, wr.created_by_user.last_name),
            "members": members_display,
        })

    return display_data

async def _get_total_workroom_count(session: AsyncSession, user_id: UUID) -> int:
    """Helper function to get the total number of workrooms the user is associated with."""
    result = await session.execute(
        select(WorkroomMemberLink.workroom_id).where(WorkroomMemberLink.user_id == user_id)
    )
    workroom_ids = result.scalars().all()
    return len(set(workroom_ids))

def format_member_name(first_name: Optional[str], last_name: Optional[str]) -> str:
    if first_name and last_name:
        return f"{first_name} {last_name}"
    if first_name:
        return first_name
    if last_name:
        return last_name
    return "Unnamed Member"

# Workroom Endpoints

@workroom_router.get("/all")
async def get_all_user_workrooms(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    1. Get all workrooms the user is associated with (creator or member) and the total count.
    """
    workroom_data = await _fetch_workroom_display_data(session, current_user.id)
    total_workrooms = await _get_total_workroom_count(session, current_user.id)
    return {"workrooms": workroom_data, "total_workrooms": total_workrooms}

@workroom_router.get("/created-by-me")
async def get_created_workrooms(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    2. Get workrooms created by the current logged-in user and the total count.
    """
    result = await session.execute(
        select(Workroom)
        .options(selectinload(Workroom.members), selectinload(Workroom.created_by_user))
        .where(Workroom.created_by == current_user.id)
    )
    created_workrooms = result.scalars().all()
    display_data = []
    for wr in created_workrooms:
        members_display = [
            {
                "name": format_member_name(member.first_name, member.last_name),
                "avatar_url": getattr(member, "avatar_url", None)
            }
            for member in wr.members[:3]
        ]
        display_data.append({
            "id": wr.id,
            "title": wr.name,
            "created_by": format_member_name(wr.created_by_user.first_name, wr.created_by_user.last_name),
            "members": members_display,
        })
    total_workrooms = await _get_total_workroom_count(session, current_user.id)
    return {"workrooms": display_data, "total_workrooms": total_workrooms}

@workroom_router.get("/shared")
async def get_shared_workrooms(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    3. Get workrooms where the user is a member but not the creator and the total count.
    """
    result = await session.execute(
        select(Workroom)
        .options(selectinload(Workroom.members), selectinload(Workroom.created_by_user))
        .where(
            Workroom.id.in_(
                select(WorkroomMemberLink.workroom_id).where(WorkroomMemberLink.user_id == current_user.id)
            ),
            Workroom.created_by != current_user.id,
        )
    )
    shared_workrooms = result.scalars().all()
    display_data = []
    for wr in shared_workrooms:
        members_display = [
            {
                "name": format_member_name(member.first_name, member.last_name),
                "avatar_url": getattr(member, "avatar_url", None)
            }
            for member in wr.members[:3]
        ]
        display_data.append({
            "id": wr.id,
            "title": wr.name,
            "created_by": format_member_name(wr.created_by_user.first_name, wr.created_by_user.last_name),
            "members": members_display,
        })
    total_workrooms = await _get_total_workroom_count(session, current_user.id)
    return {"workrooms": display_data, "total_workrooms": total_workrooms}

@workroom_router.post("", status_code=status.HTTP_201_CREATED, response_model=WorkroomSchema)
async def create_workroom(
    workroom_data: WorkroomCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    try:
        # Create the Workroom
        new_workroom = Workroom(
            name=workroom_data.name,
            created_by=current_user.id,
            kpis=workroom_data.kpis
        )
        session.add(new_workroom)
        await session.flush()

        # Add Performance Metrics
        for pm_data in workroom_data.performance_metrics:
            session.add(WorkroomPerformanceMetric(
                workroom_id=new_workroom.id,
                kpi_name=pm_data.kpi_name,
                metric_value=pm_data.metric_value,
                user_id=current_user.id,
                weight=pm_data.weight,
            ))

        # Add creator as member
        session.add(WorkroomMemberLink(
            workroom_id=new_workroom.id,
            user_id=current_user.id,
        ))

        # Process friend emails
        emails_to_invite = []
        if workroom_data.friend_emails:
            for friend_email in workroom_data.friend_emails:
                friend_user = (await session.execute(
                    select(User).filter_by(email=friend_email)
                )).scalar_one_or_none()
                
                if friend_user:
                    existing_member = (await session.execute(
                        select(WorkroomMemberLink).where(
                            WorkroomMemberLink.workroom_id == new_workroom.id,
                            WorkroomMemberLink.user_id == friend_user.id
                        )
                    )).scalar_one_or_none()
                    
                    if not existing_member:
                        session.add(WorkroomMemberLink(
                            workroom_id=new_workroom.id,
                            user_id=friend_user.id
                        ))
                else:
                    emails_to_invite.append(friend_email)

        await session.commit()

        # 5. Explicitly load relationships
        await session.refresh(new_workroom)
        result = await session.execute(
            select(Workroom)
            .where(Workroom.id == new_workroom.id)
            .options(
                selectinload(Workroom.metrics),
                selectinload(Workroom.performance_metrics),
                selectinload(Workroom.members)
            )
        )
        loaded_workroom = result.scalar_one()

        # 6. Send invites to non-members
        if emails_to_invite:
            send_workroom_invites.delay(
                workroom_name=new_workroom.name,
                creator_name=current_user.first_name or "Someone",
                recipient_emails=emails_to_invite
            )

        return loaded_workroom
        
    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

@workroom_router.patch("/{workroom_id}", response_model=WorkroomSchema)
async def update_workroom(
    workroom_id: UUID,
    workroom_update: WorkroomUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Updates an existing workroom's name, major KPI, and performance metrics.
    """
    # Load workroom with all required relationships
    stmt = (
        select(Workroom)
        .where(Workroom.id == workroom_id)
        .options(
            selectinload(Workroom.performance_metrics),
            selectinload(Workroom.members),
            selectinload(Workroom.metrics)
        )
    )
    
    result = await session.execute(stmt)
    workroom = result.scalar_one_or_none()
    
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    if workroom.created_by != current_user.id:
        raise HTTPException(
            status_code=403, detail="Not authorized to update this workroom"
        )

    update_data = workroom_update.dict(exclude_unset=True)
            
    # Update Name
    if "name" in update_data:
        workroom.name = update_data["name"]

    if "kpis" in update_data:
        workroom.kpis = update_data["kpis"]

    if "performance_metrics" in update_data:
        # Clear existing performance metrics
        await session.execute(
            delete(WorkroomPerformanceMetric).where(
                WorkroomPerformanceMetric.workroom_id == workroom_id
            )
        )
        # Add the updated performance metrics
        for pm_data in workroom_update.performance_metrics:
            pm = WorkroomPerformanceMetric(
                workroom_id=workroom_id,
                kpi_name=pm_data.kpi_name,
                metric_value=pm_data.metric_value,
                user_id=current_user.id,
                weight=pm_data.weight,
            )
            session.add(pm)

    await session.commit()
    await session.refresh(workroom)  
    return workroom

@workroom_router.get("/{workroom_id}", response_model=WorkroomDetailsSchema)
async def get_workroom_details(
    workroom_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    # Fetch the workroom
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    # Authorization check
    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this workroom")

    # Get all members
    member_results = await session.execute(
        select(User).join(WorkroomMemberLink).where(WorkroomMemberLink.workroom_id == workroom_id)
    )
    members = member_results.scalars().all()

    # Get performance metrics for all members in one query
    kpi_summaries_result = await session.execute(
        select(UserKPISummary)
        .where(UserKPISummary.workroom_id == workroom_id)
    )
    user_kpi_summaries = kpi_summaries_result.scalars().all()

    # Fetch all performance metric definitions for the workroom
    performance_metrics_result = await session.execute(
        select(WorkroomPerformanceMetric).where(WorkroomPerformanceMetric.workroom_id == workroom_id)
    )
    performance_metrics_objs = performance_metrics_result.scalars().all()
    performance_metrics = [WorkroomPerformanceMetricSchema.from_orm(metric) for metric in performance_metrics_objs]

    # Extract KPI names defined for the workroom
    expected_kpis = [metric.kpi_name for metric in performance_metrics_objs]

    # Organize kpi_breakdown per user
    metrics_by_user = {}
    for summary in user_kpi_summaries:
        raw_metrics = summary.kpi_breakdown or []
        # Map by name for quick lookup
        metric_map = {m.get("kpi_name"): m for m in raw_metrics}

        # Ensure all KPIs are present
        complete_metrics = []
        for kpi_name in expected_kpis:
            metric_data = metric_map.get(kpi_name, {})
            complete_metrics.append(
                MemberMetricSchema(
                    kpi_name=kpi_name,
                    metric_value=metric_data.get("metric_value", 0),
                    weight=metric_data.get("weight", 0),
                )
            )
        metrics_by_user[summary.user_id] = complete_metrics

    # Construct enriched member list
    full_members = []
    for member in members:
        full_members.append(
            FullMemberSchema(
                id=member.id,
                name=format_member_name(member.first_name, member.last_name),
                email=member.email,
                avatar_url=member.avatar_url,
                xp=member.xp,
                level=member.level,
                productivity=float(member.productivity),
                average_task_time=float(member.average_task_time),
                daily_active_minutes=member.daily_active_minutes,
                teamwork_collaborations=member.teamwork_collaborations,
                metrics=metrics_by_user.get(member.id, [
                    MemberMetricSchema(kpi_name=kpi_name, metric_value=0, weight=0)
                    for kpi_name in expected_kpis
                ])
            )
        )

    # Task counts
    completed_task_count = (await session.execute(
        select(func.count(Task.id))
        .where(Task.workroom_id == workroom_id, Task.status == TaskStatus.COMPLETED)
    )).scalar() or 0

    pending_task_count = (await session.execute(
        select(func.count(Task.id))
        .where(Task.workroom_id == workroom_id, Task.status == TaskStatus.PENDING)
    )).scalar() or 0

    # All tasks in workroom
    tasks_result = await session.execute(
        select(Task).where(Task.workroom_id == workroom_id)
    )
    tasks = tasks_result.scalars().all()
    task_schemas = [TaskSchema.from_orm(task) for task in tasks]
    
    # Workroom performance metrics
    performance_metrics_result = await session.execute(
        select(WorkroomPerformanceMetric).where(WorkroomPerformanceMetric.workroom_id == workroom_id)
    )
    performance_metrics = [
        WorkroomPerformanceMetricSchema.from_orm(metric)
        for metric in performance_metrics_result.scalars().all()
    ]

    # Return all enriched data
    return WorkroomDetailsSchema(
        id=workroom.id,
        name=workroom.name,
        kpis=workroom.kpis,
        members=full_members,
        completed_task_count=completed_task_count,
        pending_task_count=pending_task_count,
        tasks=task_schemas,
        performance_metrics=performance_metrics
    )

@workroom_router.delete("/{workroom_id}")
async def delete_workroom(
    workroom_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this workroom")
    await session.delete(workroom)
    await session.commit()
    return {"message": "Workroom deleted successfully"}

# Membership Management

@workroom_router.post("/{workroom_id}/members")
async def add_members_to_workroom(
    workroom_id: UUID,
    user_ids: List[UUID],
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    statement = select(Workroom).options(selectinload(Workroom.members)).where(Workroom.id == workroom_id)
    result = await session.execute(statement)
    workroom = result.scalars().first()
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to add members to this workroom")
    for user_id in user_ids:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail=f"User with ID {user_id} not found")
        if user in workroom.members:
            continue
        workroom.members.append(user)

    await session.commit()
    await session.refresh(workroom)
    return workroom

@workroom_router.get("/{workroom_id}/members")
async def get_workroom_members(
    workroom_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
    
    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this workroom")

    # Get members
    members_query = await session.execute(
        select(User)
        .join(WorkroomMemberLink)
        .where(WorkroomMemberLink.workroom_id == workroom_id)
    )
    members = members_query.scalars().all()
    
    # Fetch all performance metric definitions for the workroom
    performance_metrics_result = await session.execute(
        select(WorkroomPerformanceMetric).where(WorkroomPerformanceMetric.workroom_id == workroom_id)
    )
    performance_metrics_objs = performance_metrics_result.scalars().all()
    expected_kpis = [metric.kpi_name for metric in performance_metrics_objs]

    member_list = []

    for member in members:
        # Fetch latest KPI summary for this member in the current workroom
        kpi_summary_query = await session.execute(
            select(UserKPISummary)
            .where(UserKPISummary.user_id == member.id, UserKPISummary.workroom_id == workroom_id)
            .order_by(UserKPISummary.date.desc())
            .limit(1)
        )
        kpi_summary = kpi_summary_query.scalar()

        # Build full metric list, ensuring all KPIs are included
        metric_map = {k.get("kpi_name"): k for k in (kpi_summary.kpi_breakdown or [])} if kpi_summary else {}
        metric_schemas = []

        for kpi_name in expected_kpis:
            kpi_data = metric_map.get(kpi_name, {})
            metric_schemas.append(
                MemberMetricSchema(
                    kpi_name=kpi_name,
                    metric_value=kpi_data.get("metric_value", 0),
                    weight=kpi_data.get("weight", 0),
                )
            )

        # Add member info to list
        full_member = FullMemberSchema(
            id=member.id,
            name=f"{(member.first_name or '')} {(member.last_name or '')}".strip(),
            email=member.email,
            avatar_url=getattr(member, "avatar_url", None),
            xp=member.xp if hasattr(member, "xp") else 0,
            level=member.level if hasattr(member, "level") else 1,
            productivity=getattr(member, "productivity", 0.0),
            average_task_time=getattr(member, "average_task_time", 0.0),
            daily_active_minutes=getattr(member, "daily_active_minutes", 0),
            teamwork_collaborations=getattr(member, "teamwork_collaborations", 0),
            metrics=metric_schemas
        )
        member_list.append(full_member)

    # Task counts
    completed_task_count_query = await session.execute(
        select(func.count(Task.id))
        .where(Task.workroom_id == workroom_id, Task.status == TaskStatus.COMPLETED)
    )
    completed_task_count = completed_task_count_query.scalar() or 0

    pending_task_count_query = await session.execute(
        select(func.count(Task.id))
        .where(Task.workroom_id == workroom_id, Task.status == TaskStatus.PENDING)
    )
    pending_task_count = pending_task_count_query.scalar() or 0

    return {
        "id": workroom.id,
        "name": workroom.name,
        "members": member_list,
        "completed_task_count": completed_task_count,
        "pending_task_count": pending_task_count,
    }

@workroom_router.delete("/{workroom_id}/members", response_model=Dict[str, str])
async def remove_members_from_workroom(
    workroom_id: UUID,
    user_ids: List[UUID] = Body(..., embed=True),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    statement = (
        select(Workroom)
        .options(selectinload(Workroom.members))
        .where(Workroom.id == workroom_id)
    )
    result = await session.execute(statement)
    workroom = result.scalars().first()

    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
    if workroom.created_by != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to remove members from this workroom"
        )
    stmt = delete(WorkroomMemberLink).where(
        WorkroomMemberLink.workroom_id == workroom_id,
        WorkroomMemberLink.user_id.in_(user_ids)
    )
    await session.execute(stmt)
    await session.commit()
    return {"message": f"Users {user_ids} removed from workroom {workroom_id}"}

# Task Management (Related to Workrooms)

@workroom_router.post("/{workroom_id}/tasks", response_model=TaskSchema, status_code=status.HTTP_201_CREATED)
async def create_task_in_workroom(
    workroom_id: UUID,
    task_data: WorkroomTaskCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to create tasks in this workroom")

    task_data_dict = task_data.model_dump()
    new_task = Task(**task_data_dict)
    new_task.created_by_id = current_user.id
    new_task.workroom_id = workroom_id

    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)
    
    # ⬇️ Update levels after successful task creation
    await update_user_level(LevelCategory.LEADER, 5, current_user.id, session)
    return new_task

# Liveworkroom Session Management (Related to Workrooms)

@workroom_router.post("/{workroom_id}/start-live-session", status_code=status.HTTP_201_CREATED)
async def start_live_session(
    workroom_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Start a new live session in workroom"""
    # Verify workroom access
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    new_session = WorkroomLiveSession(
        workroom_id=workroom_id,
        screen_sharer_id=current_user.id,
        is_active=True,
        start_time=datetime.utcnow(),
    )

    session.add(new_session)
    await session.commit()
    await session.refresh(new_session)

    return {
        "session_id": str(new_session.id),
        "message": "New live session started successfully",
        "workroom_id": str(workroom_id),
        "start_time": new_session.start_time.isoformat(),
    }

@workroom_router.post("/{workroom_id}/end-live-session", status_code=status.HTTP_202_ACCEPTED)
async def end_live_session(
    workroom_id: UUID,
    session_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Trigger the background task to end a live session in the workroom."""
    try:
        # 1. Validate inputs and permissions
        workroom = await session.get(Workroom, workroom_id)
        if not workroom:
            raise HTTPException(status_code=404, detail="Workroom not found")

        live_session = await session.execute(
            select(WorkroomLiveSession)
            .where(
                WorkroomLiveSession.id == session_id,
                WorkroomLiveSession.workroom_id == workroom_id
            )
        )
        live_session = live_session.scalar_one_or_none()

        if not live_session:
            raise HTTPException(status_code=404, detail="Live session not found or doesn't belong to this workroom")

        if not live_session.is_active:
            raise HTTPException(status_code=400, detail="Live session is already ended")

        # 2. Immediately mark as ending to prevent duplicate requests
        live_session.is_ending = True
        await session.commit()

        # 3. Enqueue task with proper monitoring
        task = process_workroom_end_session.apply_async(
            kwargs={
                'workroom_id': str(workroom_id),
                'session_id': str(session_id),
                'user_id': str(current_user.id)
            },
            queue='sessions',
            priority=5,
            expires=3600
        )

        logging.info(
            f"Started session closeout task {task.id} for "
            f"workroom:{workroom_id} session:{session_id}"
        )

        return {
            "message": "Session end processing started",
            "session_id": str(session_id)
        }

    except SQLAlchemyError as e:
        await session.rollback()
        logging.error(f"Database error during session end: {str(e)}")
        raise HTTPException(status_code=500, detail="Database error occurred")

    except Exception as e:
        logging.error(f"Unexpected error ending session: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")


# --------------------------------------------------------------------------------
#  AI Analysis Endpoints
# --------------------------------------------------------------------------------
@workroom_router.post("/analyze_screenshot")
async def analyze_screenshot(
    session_id: UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """
    Receives a screenshot, uploads it to S3, and triggers the image analysis and data storage.
    """
    try:
        # Validate session exists and is active
        live_session = await session.get(WorkroomLiveSession, session_id)
        if not live_session or not live_session.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found or not active"
            )

        # Validate file type
        if not file.content_type.startswith("image/"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only image files are allowed",
            )

        timestamp = datetime.now(timezone.utc)
        file_extension = file.filename.split('.')[-1].lower()
        
        # Generate secure filename
        image_filename = (
            f"user_{current_user.id}/session_{session_id}/"
            f"screenshot_{timestamp.strftime('%Y%m%d_%H%M%S')}.{file_extension}"
        )

        try:
            file.file.seek(0)
            s3_client.upload_fileobj(
                Fileobj=file.file,
                Bucket=AWS_STORAGE_BUCKET_NAME,
                Key=image_filename,
                ExtraArgs={
                    'ContentType': file.content_type,
                    'ACL': 'public-read',
                    'Metadata': {
                        'user_id': str(current_user.id),
                        'session_id': str(session_id)
                    }
                }
            )
        except ClientError as e:
            logging.error(f"S3 upload failed: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to store image"
            )
        finally:
            await file.close()

        image_url = f"https://{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{image_filename}"

        task = process_image_and_store_task.apply_async(
            kwargs={
                'user_id': str(current_user.id),
                'session_id': str(session_id),
                'image_url': image_url,
                'image_filename': image_filename,
                'timestamp_str': timestamp.isoformat()
            },
            queue='images',
            priority=6,
            expires=3600)

        return {
            "message": "Screenshot received for processing", 
            "image_url": image_url,
            "task_id": task.id
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        )

@workroom_router.post("/analyze_audio")
async def analyze_audio(
    session_id: UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Endpoint to receive and process audio files using Celery.
    """
    try:
        timestamp = datetime.now(timezone.utc)
        user_id = current_user.id

        # Validate file type
        if not file.content_type.startswith("audio/"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only audio files are allowed",
            )

        # Upload the audio file to S3
        audio_url, audio_s3_key = await upload_audio_to_s3(
            file, user_id, session_id, timestamp.strftime("%Y%m%d_%H%M%S")
        )
        if not audio_url:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload audio to S3",
            )

        # Trigger Celery task for background processing
        process_audio_and_store_report_task.apply_async(
            kwargs={
                'user_id': str(user_id),
                'session_id': str(session_id),
                'audio_url': audio_url,
                'audio_s3_key': audio_s3_key,
                'timestamp_str': timestamp.isoformat()
            })

        return (
            {"message": "Audio received for analysis. Processing in background."},
            status.HTTP_202_ACCEPTED,
        )

    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(f"Error processing audio: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing audio: {str(e)}",
        )
        
        