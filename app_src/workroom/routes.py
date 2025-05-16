import logging
from fastapi import Body, APIRouter, File, HTTPException, Depends, UploadFile, status
from sqlalchemy import func, select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from db.db_connect import get_session
from .service import upload_audio_to_s3
from .schema import WorkroomCreate, WorkroomSchema, WorkroomTaskCreate, WorkroomUpdate
from typing import List, Dict, Optional
from uuid import UUID
from db.models import (Workroom, User, Task, TaskStatus, WorkroomLiveSession, 
                       WorkroomMemberLink, WorkroomPerformanceMetric)
from auth.dependencies import get_current_user
from tasks.schema import MemberSchema, TaskSchema, WorkroomDetailsSchema
from datetime import datetime, timezone
from manager import WebSocketManager
import boto3
from botocore.exceptions import ClientError
from config import Config
from celery_task import (
    process_image_and_store_task,
    process_audio_and_store_report_task,
    process_workroom_end_session,
    send_workroom_invite_email_task
)

manager = WebSocketManager()
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
    """Helper function to fetch and format workroom display data."""
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
        members_display = [member.first_name for member in wr.members[:3]]
        display_data.append({
            "id": wr.id,
            "title": wr.name,
            "created_by": wr.created_by_user.first_name if wr.created_by_user else None,
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
        members_display = [member.first_name for member in wr.members[:3]]
        display_data.append({
            "id": wr.id,
            "title": wr.name,
            "created_by": wr.created_by_user.first_name if wr.created_by_user else None,
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
        members_display = [member.first_name for member in wr.members[:3]]
        display_data.append({
            "id": wr.id,
            "title": wr.name,
            "created_by": wr.created_by_user.first_name if wr.created_by_user else None,
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
        # 1. Create the Workroom
        new_workroom = Workroom(
            name=workroom_data.name,
            created_by=current_user.id,
            kpis=workroom_data.kpis
        )
        session.add(new_workroom)
        await session.flush()

        # 2. Add Performance Metrics with weights
        for pm_data in workroom_data.performance_metrics:
            pm = WorkroomPerformanceMetric(
                workroom_id=new_workroom.id,
                kpi_name=pm_data.kpi_name,
                metric_value=pm_data.metric_value,
                user_id=current_user.id,
                weight=pm_data.weight,
            )
            session.add(pm)

        # 3. Add the creator as a member
        workroom_member_link = WorkroomMemberLink(
            workroom_id=new_workroom.id,
            user_id=current_user.id,
        )
        session.add(workroom_member_link)
        
        await session.commit()
        
        # Refresh and load relationships
        await session.refresh(new_workroom)
        # Load relationships one by one to be explicit
        await session.execute(
            select(Workroom)
            .where(Workroom.id == new_workroom.id)
            .options(
                selectinload(Workroom.metrics),
                selectinload(Workroom.performance_metrics),
                selectinload(Workroom.members)
            )
        )

        # 4. Invite friends via Celery task
        if workroom_data.friend_emails:
            send_workroom_invite_email_task.delay(
                workroom_id=str(new_workroom.id),
                creator_name = current_user.first_name or "Someone",
                friend_emails=workroom_data.friend_emails,
            )

        return new_workroom
        
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
    # Get workroom
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
    
    # Check authorization
    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this workroom")

    # Get members (limit to 3) with null checks
    members_query = await session.execute(
        select(User)
        .join(WorkroomMemberLink)
        .where(WorkroomMemberLink.workroom_id == workroom_id)
        .limit(3)
    )
    members = members_query.scalars().all()
    
    # Safe member name construction
    member_list = [
        MemberSchema(
            name=format_member_name(member.first_name, member.last_name),
            image_url=member.avatar_url or ""
        )
        for member in members
    ]

    # Get task counts with null safety
    completed_task_count = (await session.execute(
        select(func.count(Task.id))
        .where(Task.workroom_id == workroom_id, Task.status == TaskStatus.COMPLETED)
    )).scalar() or 0

    pending_task_count = (await session.execute(
        select(func.count(Task.id))
        .where(Task.workroom_id == workroom_id, Task.status == TaskStatus.PENDING)
    )).scalar() or 0
    
    # Get all tasks in the workroom
    tasks = (await session.execute(
        select(Task)
        .where(Task.workroom_id == workroom_id)
    )).scalars().all()
    
    # Convert tasks to TaskSchema
    task_schemas = [TaskSchema.from_orm(task) for task in tasks]

    return WorkroomDetailsSchema(
        id=workroom.id,
        name=workroom.name,
        members=member_list,
        completed_task_count=completed_task_count,
        pending_task_count=pending_task_count,
        tasks=task_schemas
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
    
    #  Check authorization.  Only the workroom creator can access this endpoint
    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this workroom")

    # Get members
    members_query = await session.execute(
        select(User)
        .join(WorkroomMemberLink)
        .where(WorkroomMemberLink.workroom_id == workroom_id)
    )
    members = members_query.scalars().all()
    member_list = [
        MemberSchema(
            name=f"{(member.first_name or '')} {(member.last_name or '')}".strip(),
            image_url=member.image_url if hasattr(member, 'image_url') else None
        )
        for member in members
    ]

    # Get task counts
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

@workroom_router.post("/{workroom_id}/end-live-session")
async def end_live_session(
    workroom_id: UUID,
    session_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Trigger the background task to end a live session in the workroom."""
    user_id = current_user.id
    # 1. Check if workroom exists
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    # 2. Check if live session exists
    live_session_result = await session.execute(
        select(WorkroomLiveSession).where(WorkroomLiveSession.id == session_id)
    )
    live_session = live_session_result.scalar_one_or_none()

    if not live_session:
        raise HTTPException(status_code=404, detail="Live session not found.")

    # 3. Validate ownership and activity
    if live_session.workroom_id != workroom_id:
        raise HTTPException(status_code=400, detail="Session does not belong to this workroom.")

    if not live_session.is_active:
        raise HTTPException(status_code=400, detail="Live session is not active.")

    # 4. Trigger background task to end session
    process_workroom_end_session.delay(str(workroom_id), str(session_id), str(user_id))

    return {
        "message": "Session end initiated. Processing will continue in background.",
        "session_id": str(session_id)
    }


# --------------------------------------------------------------------------------
#  AI Analysis Endpoints
# --------------------------------------------------------------------------------
@workroom_router.post("/analyze_screenshot")
async def analyze_screenshot(
    session_id: UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Receives a screenshot, uploads it to S3, and triggers the image analysis and data storage.
    """
    try:
        timestamp = datetime.now(timezone.utc)
        user_id = current_user.id

        # Validate file type
        if not file.content_type.startswith("image/"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only image files are allowed",
            )

        # Upload to S3
        image_filename = (
            f"user_{user_id}/session_{session_id}/screenshot_"
            f"{timestamp.strftime('%Y%m%d_%H%M%S')}.{file.filename.split('.')[-1]}"
        )

        try:
            s3_client.upload_fileobj(
                Fileobj=file.file,
                Bucket=AWS_STORAGE_BUCKET_NAME,
                Key=image_filename,
                ExtraArgs={"ContentType": file.content_type, "ACL": "public-read"},
            )
        except ClientError as e:
            logging.error(f"S3 upload error: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"S3 upload error: {str(e)}",
            )

        image_url = f"https://{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{image_filename}"

        process_image_and_store_task.delay(
            str(user_id),
            str(session_id),
            image_url,
            image_filename,
            timestamp.isoformat(),
        )

        return (
            {"message": "Screenshot received for processing", "image_url": image_url},
            status.HTTP_202_ACCEPTED,
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(f"Error processing screenshot: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing screenshot: {str(e)}",
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
        process_audio_and_store_report_task.delay(
            str(user_id),
            str(session_id),
            audio_url,
            audio_s3_key,
            timestamp.isoformat(),
        )

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
        
        