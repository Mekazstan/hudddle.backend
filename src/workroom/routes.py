from fastapi import Body, APIRouter, HTTPException, Depends, status, Query
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from db.db_connect import get_session
from .service import update_workroom_leaderboard
from .schema import WorkroomCreate, WorkroomSchema, WorkroomTaskCreate, WorkroomUpdate
from typing import List, Optional, Dict, Any
from uuid import UUID
from db.models import Workroom, User, Task, Leaderboard, TaskStatus, WorkroomMemberLink
from auth.dependencies import get_current_user
from auth.schema import UserSchema
from tasks.schema import TaskSchema
from datetime import datetime
from manager import WebSocketManager

manager = WebSocketManager()


workroom_router = APIRouter()

# Workroom Endpoints

@workroom_router.post("", status_code=status.HTTP_201_CREATED)
async def create_workroom(
    workroom_data: WorkroomCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    workroom_data_dict = workroom_data.model_dump()
    new_workroom = Workroom(**workroom_data_dict)
    new_workroom.created_by = current_user.id
    session.add(new_workroom)
    await session.commit()
    await session.refresh(new_workroom)

    # Add the creator as a member of the workroom
    workroom_member_link = WorkroomMemberLink(
        workroom_id=new_workroom.id,
        user_id=current_user.id
    )
    session.add(workroom_member_link)
    await session.commit()
    return new_workroom

@workroom_router.get("", response_model=List[WorkroomSchema])
async def get_workrooms(session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    result = await session.execute(select(Workroom).where(Workroom.created_by == current_user.id))
    workrooms = result.scalars().all()
    return workrooms

@workroom_router.get("/{workroom_id}", response_model=WorkroomSchema)
async def get_workroom(
    workroom_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this workroom")
    return workroom

@workroom_router.patch("/{workroom_id}", response_model=WorkroomSchema)
async def update_workroom(
    workroom_id: UUID,
    workroom_update: WorkroomUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to update this workroom")

    for key, value in workroom_update.dict(exclude_unset=True).items():
        setattr(workroom, key, value)
    await session.commit()
    await session.refresh(workroom)
    return workroom

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

@workroom_router.get("/{workroom_id}/members", response_model=List[UserSchema])
async def get_workroom_members(
    workroom_id: UUID,
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
        raise HTTPException(status_code=403, detail="Not authorized to view members of this workroom")

    return workroom.members

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

@workroom_router.get("/{workroom_id}/tasks", response_model=List[TaskSchema])
async def get_workroom_tasks(
    workroom_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    status: Optional[TaskStatus] = Query(None, description="Filter by task status"),
    due_date: Optional[datetime] = Query(None, description="Filter by due date"),
):
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access tasks for this workroom")

    query = select(Task).where(Task.workroom_id == workroom_id)

    if status:
        query = query.where(Task.status == status)
    if due_date:
        query = query.where(Task.due_date == due_date)

    result = await session.execute(query)
    tasks = result.scalars().all()
    return tasks

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

@workroom_router.patch("/{workroom_id}/tasks/{task_id}", response_model=TaskSchema)
async def add_existing_task_to_workroom(
    workroom_id: UUID,
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workroom not found")

    if workroom.created_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workroom creator can add tasks to this workroom."
        )

    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    if task.workroom_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Task already belongs to a workroom."
        )

    task.workroom_id = workroom_id
    await session.commit()
    await session.refresh(task)
    return task

# Leaderboard Management (Related to Workrooms)

@workroom_router.get("/{workroom_id}/leaderboard", response_model=List[Dict[str, Any]])
async def get_workroom_leaderboard(
    workroom_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
    if current_user not in workroom.members:
        raise HTTPException(status_code=403, detail="Not authorized to view the leaderboard for this workroom")

    await update_workroom_leaderboard(workroom_id, session)

    result = await session.execute(select(Leaderboard).where(Leaderboard.workroom_id == workroom_id).order_by(Leaderboard.rank))
    leaderboard_entries = result.scalars().all()

    return [{
        "user_id": entry.user_id,
        "username": entry.user.username,
        "score": entry.score,
        "teamwork_score": entry.teamwork_score,
        "rank": entry.rank,
    } for entry in leaderboard_entries]


@workroom_router.get("/{workroom_id}/live-session", response_model=Dict)
async def get_live_session_info(
    workroom_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Get information about active live session in workroom"""
    # Verify workroom access
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
        
    if not await manager.verify_workroom_access(str(current_user.id), str(workroom_id), session):
        raise HTTPException(status_code=403, detail="No access to this workroom")
    
    # Get live session info
    live_session = await manager.get_or_create_live_session(str(workroom_id), session)
    
    # Get participants
    participants = []
    for user_id in manager.active_connections.get(str(workroom_id), {}).keys():
        user_data = await manager.get_user_data(user_id, session)
        participants.append(user_data)
    
    # Get screen sharer data if any
    screen_sharer_data = None
    if live_session.screen_sharer_id:
        screen_sharer_data = await manager.get_user_data(live_session.screen_sharer_id, session)
    
    return {
        "session_id": str(live_session.id),
        "is_active": live_session.is_active,
        "screen_sharer": screen_sharer_data,
        "participants": participants,
        "started_at": live_session.created_at.isoformat() if live_session.created_at else None,
        "workroom_id": str(workroom_id)
    }

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
        
    if not await manager.verify_workroom_access(str(current_user.id), str(workroom_id), session):
        raise HTTPException(status_code=403, detail="No access to this workroom")
    
    # Create or get existing session
    live_session = await manager.get_or_create_live_session(str(workroom_id), session)
    
    return {
        "session_id": str(live_session.id),
        "message": "Live session started",
        "workroom_id": str(workroom_id)
    }

@workroom_router.post("/{workroom_id}/end-live-session")
async def end_live_session(
    workroom_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """End the active live session in workroom"""
    # Verify workroom access and ownership
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
        
    if workroom.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only workroom creator can end session")
    
    # End session
    await manager.end_live_session(str(workroom_id), session)
    
    return {
        "message": "Live session ended",
        "workroom_id": str(workroom_id)
    }
    
@workroom_router.post("/{workroom_id}/request-access")
async def request_workroom_access(
    workroom_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Request access to a workroom"""
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")
    
    # Check if already a member
    result = await session.execute(
        select(WorkroomMemberLink)
        .where(
            WorkroomMemberLink.workroom_id == workroom_id,
            WorkroomMemberLink.user_id == current_user.id
        )
    )
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Already a member of this workroom")
    
    # Here you would typically:
    # 1. Create a notification for the workroom owner
    # 2. Send an email to the owner
    # 3. Log the access request
    
    return {"message": "Access request sent to workroom owner"}
