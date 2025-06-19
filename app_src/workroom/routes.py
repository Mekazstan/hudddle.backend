import logging
from fastapi import (Body, APIRouter, File, HTTPException, 
                     Depends, UploadFile, status)
from sqlalchemy import exists, func, select, delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app_src.achievements.service import update_user_level
from app_src.db.db_connect import get_session
from app_src.redis_config import get_redis_pool
from .service import upload_audio_to_s3
from .schema import (FullMemberSchema, LeaderboardEntrySchema, MemberMetricSchema, UserKPIMetricHistorySchema, UserKPISummarySchema, WorkroomCreate, WorkroomDetailsSchema, WorkroomKPIMetricHistorySchema, WorkroomKPISummarySchema, WorkroomPerformanceMetricSchema, 
                     WorkroomSchema, WorkroomTaskCreate, WorkroomUpdate)
from typing import List, Dict, Optional
from uuid import UUID
from app_src.db.models import (Leaderboard, LevelCategory, UserKPIMetricHistory, UserKPISummary, Workroom, User, 
                               Task, TaskStatus, WorkroomKPIMetricHistory, WorkroomKPISummary, WorkroomLiveSession, 
                       WorkroomMemberLink, WorkroomPerformanceMetric)
from app_src.auth.dependencies import get_current_user
from app_src.tasks.schema import TaskSchema
from datetime import datetime, timezone, date
import boto3
from botocore.exceptions import ClientError
from app_src.config import Config
from arq.connections import ArqRedis


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

async def initialize_kpi_data_for_workroom(session, workroom: Workroom, members: list[User], performance_metrics: list[WorkroomPerformanceMetric]):
    today = date.today()
    kpi_names = [metric.kpi_name for metric in performance_metrics]
    kpi_weight = [metric.weight for metric in performance_metrics]

    # 1. Create default WorkroomKPISummary
    session.add(WorkroomKPISummary(
        workroom_id=workroom.id,
        date=today,
        overall_alignment_percentage=0.0,
        summary_text=f"No summary for {workroom.name}",
        kpi_breakdown=[{"kpi_name": kpi, "percentage": 0, "weight": weight} for kpi, weight in zip(kpi_names, kpi_weight)]
    ))

    # 2. Create default WorkroomKPIMetricHistory entries
    session.add(WorkroomKPIMetricHistory(
        workroom_id=workroom.id,
        kpi_name=f"{today} - Overall Alignment",
        date=today,
        metric_value=0.0
    ))

    # 3. For each user: UserKPISummary + UserKPIMetricHistory
    for member in members:
        session.add(UserKPISummary(
            user_id=member.id,
            workroom_id=workroom.id,
            date=today,
            overall_alignment_percentage=0.0,
            summary_text=f"No summary for {member.first_name or 'User'}",
            kpi_breakdown=[{"kpi_name": kpi, "percentage": 0, "weight": weight} for kpi, weight in zip(kpi_names, kpi_weight)]
        ))

        session.add(UserKPIMetricHistory(
            user_id=member.id,
            workroom_id=workroom.id,
            kpi_name=f"{today} - Overall Alignment",
            date=today,
            alignment_percentage=0.0
        ))

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
    redis: ArqRedis = Depends(get_redis_pool),
):
    try:
        # Create the Workroom
        new_workroom = Workroom(
            name=workroom_data.name,
            created_by=current_user.id
        )
        session.add(new_workroom)
        await session.flush()

        # Add Performance Metrics
        for pm_data in workroom_data.performance_metrics:
            session.add(WorkroomPerformanceMetric(
                workroom_id=new_workroom.id,
                kpi_name=pm_data.kpi_name,
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
                selectinload(Workroom.performance_metrics),
                selectinload(Workroom.members),
                selectinload(Workroom.leaderboards)
            )
        )
        loaded_workroom = result.scalar_one()
        
        # 6. Fetch all members
        all_members_result = await session.execute(
            select(User).join(WorkroomMemberLink).where(WorkroomMemberLink.workroom_id == new_workroom.id)
        )
        all_members = all_members_result.scalars().all()
        
        # Create default leaderboard entries for all members
        base_score = 30  # Starting score for the creator
        for i, member in enumerate(all_members):
            # Create progressively lower scores for other members
            member_score = max(base_score - (i * 3), 2)  # Ensure minimum score of 20
            
            # Calculate component scores (these can be adjusted based on your scoring logic)
            kpi_score = round(member_score * 0.5, 1)    # 50% of total score
            task_score = round(member_score * 0.3, 1)   # 30% of total score
            teamwork_score = round(member_score * 0.15, 1)  # 15% of total score
            engagement_score = round(member_score * 0.05, 1)  # 5% of total score
            
            session.add(Leaderboard(
                workroom_id=new_workroom.id,
                user_id=member.id,
                score=member_score,
                kpi_score=kpi_score,
                task_score=task_score,
                teamwork_score=teamwork_score,
                engagement_score=engagement_score,
                rank=i + 1
            ))
            
        # 7. Fetch all performance metrics just created
        performance_metrics_result = await session.execute(
            select(WorkroomPerformanceMetric).where(WorkroomPerformanceMetric.workroom_id == new_workroom.id)
        )
        created_metrics = performance_metrics_result.scalars().all()
        
        # 8. Initialize KPI structures
        # await initialize_kpi_data_for_workroom(session, new_workroom, all_members, created_metrics)

        # 9. Send invites to non-members
        if emails_to_invite:
            await redis.enqueue_job(
                'send_workroom_invites',
                new_workroom.name,
                current_user.first_name or "Someone",
                emails_to_invite
            )

        await session.commit()
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
            selectinload(Workroom.members)
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
                user_id=current_user.id,
                weight=pm_data.weight,
            )
            session.add(pm)

    await session.commit()
    await session.refresh(workroom)  
    return workroom

@workroom_router.get("/{workroom_id}")
async def get_workroom_details(
    workroom_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    # Fetch the workroom with leaderboard relationships
    workroom_result = await session.execute(
        select(Workroom)
        .options(
            selectinload(Workroom.leaderboards).joinedload(Leaderboard.user),
            selectinload(Workroom.members)
        )
        .where(Workroom.id == workroom_id)
    )
    workroom = workroom_result.scalar_one_or_none()
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    # Authorization check
    result = await session.execute(
        select(exists().where(
            WorkroomMemberLink.workroom_id == workroom_id,
            WorkroomMemberLink.user_id == current_user.id
        ))
    )
    if not result.scalar():
        raise HTTPException(status_code=403, detail="Not authorized to access this workroom")

    # Process leaderboard data
    if workroom.leaderboards:
        leaderboard_data = []
        for leaderboard in workroom.leaderboards:
            leaderboard_data.append({
                "user_id": leaderboard.user_id,
                "user_name": format_member_name(leaderboard.user.first_name, leaderboard.user.last_name),
                "avatar_url": leaderboard.user.avatar_url,
                "score": leaderboard.score,
                "rank": leaderboard.rank,
                "kpi_score": leaderboard.kpi_score,
                "task_score": leaderboard.task_score,
                "teamwork_score": leaderboard.teamwork_score,
                "engagement_score": leaderboard.engagement_score
            })

        # Sort leaderboard by score (descending) and calculate ranks
        leaderboard_data.sort(key=lambda x: x["score"], reverse=True)
        for i, entry in enumerate(leaderboard_data, start=1):
            entry["rank"] = i
    else:
        # Generate default leaderboard data based on workroom members
        members_result = await session.execute(
            select(User).join(WorkroomMemberLink).where(WorkroomMemberLink.workroom_id == workroom_id)
        )
        members = members_result.scalars().all()
        
        leaderboard_data = []
        base_score = 30  # Starting score for default data
        for i, member in enumerate(members):
            # Create progressively lower scores for default ranking
            member_score = max(base_score - (i * 3), 2)  # Ensure minimum score of 20
            leaderboard_data.append({
                "user_id": member.id,
                "user_name": format_member_name(member.first_name, member.last_name),
                "avatar_url": member.avatar_url,
                "score": member_score,
                "rank": i + 1,
                "kpi_score": round(member_score * 0.5, 1),  # KPI contributes 50%
                "task_score": round(member_score * 0.3, 1),  # Tasks contribute 30%
                "teamwork_score": round(member_score * 0.15, 1),  # Teamwork 15%
                "engagement_score": round(member_score * 0.05, 1)  # Engagement 5%
            })

    # Get all members (rest of your existing member processing code remains the same)
    member_results = await session.execute(
        select(User).join(WorkroomMemberLink).where(WorkroomMemberLink.workroom_id == workroom_id)
    )
    members = member_results.scalars().all()

    # Get user KPI summaries
    kpi_summaries_result = await session.execute(
        select(UserKPISummary).where(UserKPISummary.workroom_id == workroom_id)
    )
    user_kpi_summaries = kpi_summaries_result.scalars().all()
    summary_by_user = {s.user_id: s for s in user_kpi_summaries}

    # Get all KPI metric definitions for the workroom
    performance_metrics_result = await session.execute(
        select(WorkroomPerformanceMetric).where(WorkroomPerformanceMetric.workroom_id == workroom_id)
    )
    performance_metrics_objs = performance_metrics_result.scalars().all()
    performance_metrics = [WorkroomPerformanceMetricSchema.from_orm(metric) for metric in performance_metrics_objs]
    expected_kpis = [metric.kpi_name for metric in performance_metrics_objs]

    kpi_weight_map = {metric.kpi_name: metric.weight for metric in performance_metrics_objs}
    
    # Build user metrics map
    metrics_by_user = {}
    for summary in user_kpi_summaries:
        raw_metrics = summary.kpi_breakdown or []
        metric_map = {}
        for m in raw_metrics:
            if isinstance(m, dict):
                if "kpi_name" in m:
                    metric_map[m["kpi_name"]] = m

        complete_metrics = []
        for kpi_name in expected_kpis:
            metric_data = metric_map.get(kpi_name, {})
            complete_metrics.append(
                {
                    "kpi_name":kpi_name,
                    "percentage":metric_data.get("percentage", 0)
                }
            )
        metrics_by_user[summary.user_id] = complete_metrics

    # Fetch user KPI metric history (for line chart)
    user_history_result = await session.execute(
        select(UserKPIMetricHistory).where(UserKPIMetricHistory.workroom_id == workroom_id)
    )
    user_metric_histories = user_history_result.scalars().all()

    history_by_user: dict[UUID, list[UserKPIMetricHistory]] = {}
    for record in user_metric_histories:
        history_by_user.setdefault(record.user_id, []).append(record)

    # Build full member list
    full_members = []
    for member in members:
        user_metrics = metrics_by_user.get(member.id, [])
        summary = summary_by_user.get(member.id)

        # Find leaderboard entry for this user
        leaderboard_entry = None
        if leaderboard_data:
            user_entry = next(
                (entry for entry in leaderboard_data if entry["user_id"] == member.id),
                None
            )
            if user_entry:
                leaderboard_entry = LeaderboardEntrySchema(
                    score=user_entry["score"],
                    rank=user_entry["rank"],
                    kpi_score=user_entry["kpi_score"],
                    task_score=user_entry["task_score"],
                    teamwork_score=user_entry.get("teamwork_score"),
                    engagement_score=user_entry.get("engagement_score")
                )

        kpi_summary = None
        if summary is not None:
            kpi_summary = {
                "overall_alignment_percentage": summary.overall_alignment_percentage,
                "summary_text": summary.summary_text,
                "kpi_breakdown": summary.kpi_breakdown
            }
        else:
            kpi_summary = {
                "overall_alignment_percentage": 0.0,
                "summary_text": f"No summary for {member.first_name or 'User'}",
                "kpi_breakdown": [
                    {"kpi_name":kpi, "percentage":0.0, "weight": kpi_weight_map.get(kpi, 0.0)}
                    for kpi in expected_kpis
                ]
            }

        # Build KPI metric history for member
        if history_by_user.get(member.id):
            kpi_metric_history = [
                UserKPIMetricHistorySchema(
                    kpi_name=record.kpi_name,
                    date=record.date,
                    alignment_percentage=record.alignment_percentage
                )
                for record in history_by_user[member.id]
            ]
        else:
            # Provide default KPI metric history if none found
            kpi_metric_history = [
                {
                    "kpi_name":"Overall KPI Alignment",
                    "date":date.today(),
                    "alignment_percentage":0.0
                }
            ]

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
                kpi_summary=kpi_summary,
                kpi_metric_history=kpi_metric_history,
                leaderboard_data=leaderboard_entry
            )
        )

    # Completed and pending task counts
    completed_task_count = (await session.execute(
        select(func.count(Task.id))
        .where(Task.workroom_id == workroom_id, Task.status == TaskStatus.COMPLETED)
    )).scalar() or 0

    pending_task_count = (await session.execute(
        select(func.count(Task.id))
        .where(Task.workroom_id == workroom_id, Task.status == TaskStatus.PENDING)
    )).scalar() or 0

    # All tasks in the workroom
    tasks_result = await session.execute(
        select(Task).where(Task.workroom_id == workroom_id)
    )
    tasks = tasks_result.scalars().all()
    task_schemas = [TaskSchema.from_orm(task) for task in tasks]

    # Workroom KPI summary
    wr_kpi_summary_result = await session.execute(
        select(WorkroomKPISummary).where(WorkroomKPISummary.workroom_id == workroom_id)
        .order_by(WorkroomKPISummary.date.desc()).limit(1)
    )
    wr_kpi_summary = wr_kpi_summary_result.scalar_one_or_none()

    workroom_kpi_summary = None
    if wr_kpi_summary is not None:
        workroom_kpi_summary = {
            "overall_alignment_percentage": wr_kpi_summary.overall_alignment_percentage,
            "summary_text": wr_kpi_summary.summary_text,
            "kpi_breakdown": wr_kpi_summary.kpi_breakdown 
        }
    else:
        workroom_kpi_summary = {
            "overall_alignment_percentage": 0.0,
            "summary_text": f"No summary for {workroom.name}",
            "kpi_breakdown": [
                {
                    "kpi_name": metrics.kpi_name, 
                    "weight": metrics.weight,
                    "metric_value":0.0
                } for metrics in performance_metrics
            ]
        }

    # Workroom KPI metric history (line chart)
    wr_metric_history_result = await session.execute(
        select(WorkroomKPIMetricHistory).where(WorkroomKPIMetricHistory.workroom_id == workroom_id)
    )
    wr_metric_history = wr_metric_history_result.scalars().all()

    if wr_metric_history:
        workroom_kpi_metric_history = [
            WorkroomKPIMetricHistorySchema(
                kpi_name=record.kpi_name,
                date=record.date,
                alignment_percentage=record.metric_value
            )
            for record in wr_metric_history
        ]
    else:
        workroom_kpi_metric_history = [
            {
                "kpi_name": "Overall KPI Alignment",
                "date": date.today(),
                "alignment_percentage": 0.0
            }
        ]

    return {
        "id": workroom.id,
        "name": workroom.name,
        "members": full_members,
        "completed_task_count": completed_task_count,
        "pending_task_count": pending_task_count,
        "tasks": task_schemas,
        "performance_metrics": performance_metrics,
        "workroom_kpi_summary": workroom_kpi_summary,
        "workroom_kpi_metric_history": workroom_kpi_metric_history,
        "leaderboard": leaderboard_data
    }

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


@workroom_router.get("/{workroom_id}/active-session")
async def get_workroom_active_session(
    workroom_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Get the active session for a workroom (where is_active == True).
    Only the workroom creator can access this endpoint.
    Returns the session details and owner email.
    Returns 404 if no active session exists.
    """
    # First verify the user is the creator of this workroom
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workroom not found"
        )
    
    if workroom.created_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workroom creator can access active sessions"
        )

    # Get the active session
    active_session = await session.execute(
        select(WorkroomLiveSession)
        .where(
            WorkroomLiveSession.workroom_id == workroom_id,
            WorkroomLiveSession.is_active == True
        )
        .limit(1)
    )
    
    active_session = active_session.scalar_one_or_none()
    
    if not active_session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active session found for this workroom"
        )

    # Get the session owner's email
    session_owner_email = None
    if active_session.screen_sharer_id:
        owner = await session.get(User, active_session.screen_sharer_id)
        session_owner_email = owner.email if owner else None

    return {
        "session_id": active_session.id,
        "workroom_id": active_session.workroom_id,
        "start_time": active_session.start_time,
        "is_active": active_session.is_active,
        "session_owner_email": session_owner_email
    }

# Membership Management

@workroom_router.post("/{workroom_id}/members")
async def add_members_to_workroom(
    workroom_id: UUID,
    emails: List[str] = Body(..., embed=True),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis: ArqRedis = Depends(get_redis_pool)
):
    today = date.today()
    statement = select(Workroom).options(selectinload(Workroom.members)).where(Workroom.id == workroom_id)
    result = await session.execute(statement)
    workroom = result.scalars().first()
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to add members to this workroom")
    
    # Track emails of users not found in database
    non_member_emails = []
    
    # Fetch once before the for-loop
    performance_metrics_result = await session.execute(
        select(WorkroomPerformanceMetric).where(WorkroomPerformanceMetric.workroom_id == workroom_id)
    )
    performance_metrics = performance_metrics_result.scalars().all()
    
    for email in emails:
        # Convert email to lowercase
        email_lower = email.lower()
        
        # Find user by email
        user = (await session.execute(
            select(User).where(func.lower(User.email) == email_lower)
        )).scalar_one_or_none()
        
        if user:
            # Check if user is already a member
            existing_member = (await session.execute(
                select(WorkroomMemberLink).where(
                    WorkroomMemberLink.workroom_id == workroom_id,
                    WorkroomMemberLink.user_id == user.id
                )
            )).scalar_one_or_none()
            
            if not existing_member:
                # Add user to workroom
                session.add(WorkroomMemberLink(
                    workroom_id=workroom_id,
                    user_id=user.id
                ))
            # # Build default KPI breakdown
            # kpi_breakdown = [
            #     {"kpi_name": metric.kpi_name, "percentage": 0, "weight": metric.weight} for metric in performance_metrics
            # ]

            # # Create UserKPISummary
            # session.add(UserKPISummary(
            #     user_id=user.id,
            #     workroom_id=workroom_id,
            #     date=datetime.utcnow().date(),
            #     overall_alignment_percentage=0,
            #     kpi_breakdown=kpi_breakdown,
            #     summary_text=f"No summary for {user.first_name or user.email}"
            # ))

            # # Create UserKPIMetricHistory entries
            # session.add(UserKPIMetricHistory(
            #     user_id=user.id,
            #     workroom_id=workroom_id,
            #     kpi_name=f"{today} - Overall Alignment",
            #     date=datetime.utcnow().date(),
            #     alignment_percentage=0
            # ))

        else:
            non_member_emails.append(email_lower)

    await session.commit()
    
    # Send invites to non-member emails if any
    if non_member_emails:
        await redis.enqueue_job(
            'send_workroom_invites',
            workroom.name,
            current_user.first_name or "Someone",
            non_member_emails
        )
    
    # Refresh and return updated workroom
    await session.refresh(workroom)
    return {
        "message": "Members added successfully",
        "workroom": workroom,
        "non_member_emails": non_member_emails
    }

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

    # Get performance metrics
    performance_metrics_result = await session.execute(
        select(WorkroomPerformanceMetric).where(WorkroomPerformanceMetric.workroom_id == workroom_id)
    )
    performance_metrics_objs = performance_metrics_result.scalars().all()
    expected_kpis = [metric.kpi_name for metric in performance_metrics_objs]
    kpi_weight_map = {metric.kpi_name: metric.weight for metric in performance_metrics_objs}

    # Fetch KPI summaries for all users in this workroom
    kpi_summaries_result = await session.execute(
        select(UserKPISummary).where(UserKPISummary.workroom_id == workroom_id)
    )
    user_kpi_summaries = kpi_summaries_result.scalars().all()
    summary_by_user = {s.user_id: s for s in user_kpi_summaries}

    # Fetch KPI metric histories for all users in this workroom
    user_history_result = await session.execute(
        select(UserKPIMetricHistory).where(UserKPIMetricHistory.workroom_id == workroom_id)
    )
    user_metric_histories = user_history_result.scalars().all()

    history_by_user: dict[UUID, list[UserKPIMetricHistory]] = {}
    for record in user_metric_histories:
        history_by_user.setdefault(record.user_id, []).append(record)

    member_list = []

    for member in members:
        summary = summary_by_user.get(member.id)
        
        if summary:
            # Safe processing of kpi_breakdown
            processed_breakdown = []
            if summary.kpi_breakdown:
                for metric in summary.kpi_breakdown:
                    if isinstance(metric, dict):
                        try:
                            processed_breakdown.append(MemberMetricSchema(**metric))
                        except Exception as e:
                            logging.warning(f"Skipping invalid metric for user {member.id}: {metric}. Error: {e}")
            
            kpi_summary = UserKPISummarySchema(
                overall_alignment_percentage=summary.overall_alignment_percentage,
                summary_text=summary.summary_text,
                kpi_breakdown=processed_breakdown
            )
        else:
            kpi_summary = {
                "overall_alignment_percentage": 0.0,
                "summary_text": f"No summary for {member.first_name or 'User'}",
                "kpi_breakdown": [
                    {"kpi_name": kpi, "percentage": 0.0, "weight": kpi_weight_map.get(kpi, 0.0)}
                    for kpi in expected_kpis
                ]
            }

        # KPI metric history
        if history_by_user.get(member.id):
            kpi_metric_history = [
                UserKPIMetricHistorySchema(
                    kpi_name=record.kpi_name,
                    date=record.date,
                    alignment_percentage=record.alignment_percentage
                )
                for record in history_by_user[member.id]
            ]
        else:
            kpi_metric_history = [
                {
                    "kpi_name": "Overall KPI Alignment",
                    "date": date.today(),
                    "alignment_percentage": 0.0
                }
            ]

        full_member = {
            "id": member.id,
            "name": f"{(member.first_name or '')} {(member.last_name or '')}".strip(),
            "email": member.email,
            "avatar_url": getattr(member, "avatar_url", None),
            "xp": member.xp if hasattr(member, "xp") else 0,
            "level": member.level if hasattr(member, "level") else 1,
            "productivity": getattr(member, "productivity", 0.0),
            "average_task_time": getattr(member, "average_task_time", 0.0),
            "daily_active_minutes": getattr(member, "daily_active_minutes", 0),
            "teamwork_collaborations": getattr(member, "teamwork_collaborations", 0),
            "kpi_summary": kpi_summary,
            "kpi_metric_history": kpi_metric_history
        }
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
    redis: ArqRedis = Depends(get_redis_pool)
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
        job = await redis.enqueue_job(
            'process_workroom_end_session',
            str(workroom_id),
            str(session_id),
            str(current_user.id)
        )

        logging.info(
            f"Started session closeout job {job.job_id} for "
            f"workroom:{workroom_id} session:{session_id}"
        )

        return {
            "message": "Session end processing started",
            "session_id": str(session_id),
            "job_id": job.job_id
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
    session: AsyncSession = Depends(get_session),
    redis: ArqRedis = Depends(get_redis_pool)
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

        # Enqueue the image processing task
        job = await redis.enqueue_job(
            'process_image_and_store_task',
            str(current_user.id),
            str(session_id),
            image_url,
            image_filename,
            timestamp.isoformat()
        )

        return {
            "message": "Screenshot received for processing", 
            "image_url": image_url,
            "job_id": job.job_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        )

# @workroom_router.post("/analyze_audio")
# async def analyze_audio(
#     session_id: UUID,
#     file: UploadFile = File(...),
#     current_user: User = Depends(get_current_user),
# ):
#     """
#     Endpoint to receive and process audio files using Celery.
#     """
#     try:
#         timestamp = datetime.now(timezone.utc)
#         user_id = current_user.id

#         # Validate file type
#         if not file.content_type.startswith("audio/"):
#             raise HTTPException(
#                 status_code=status.HTTP_400_BAD_REQUEST,
#                 detail="Only audio files are allowed",
#             )

#         # Upload the audio file to S3
#         audio_url, audio_s3_key = await upload_audio_to_s3(
#             file, user_id, session_id, timestamp.strftime("%Y%m%d_%H%M%S")
#         )
#         if not audio_url:
#             raise HTTPException(
#                 status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#                 detail="Failed to upload audio to S3",
#             )

#         # Trigger Celery task for background processing
#         process_audio_and_store_report_task.apply_async(
#             kwargs={
#                 'user_id': str(user_id),
#                 'session_id': str(session_id),
#                 'audio_url': audio_url,
#                 'audio_s3_key': audio_s3_key,
#                 'timestamp_str': timestamp.isoformat()
#             })

#         return (
#             {"message": "Audio received for analysis. Processing in background."},
#             status.HTTP_202_ACCEPTED,
#         )

#     except HTTPException as e:
#         raise e
#     except Exception as e:
#         logging.error(f"Error processing audio: {e}")
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail=f"Error processing audio: {str(e)}",
#         )
        
        