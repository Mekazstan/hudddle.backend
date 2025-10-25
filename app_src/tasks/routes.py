import json
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone
from typing import List
from uuid import UUID
from app_src.achievements.service import update_user_level
from app_src.db.db_connect import get_session
from .schema import TaskCreate, TaskSchema, TaskUpdate
from app_src.db.models import (FriendLink, LevelCategory, Task, TaskCollaborator, TaskStatus,
                       User, Workroom, WorkroomMemberLink)
from app_src.auth.dependencies import get_current_user

task_router = APIRouter()

# Task Endpoints

@task_router.post("/{task_id}/invite-friend/{friend_id}", status_code=status.HTTP_201_CREATED)
async def invite_friend_to_task(
    task_id: UUID,
    friend_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Invite a friend to collaborate on a task.
    
    Points awarded:
    - Inviter gets +3 TEAM_PLAYER points for the invite
    - Invitee will get +3 TEAM_PLAYER points when they accept (handled elsewhere)
    - Both get +4 TEAM_PLAYER points for the collaboration (handled on task completion)
    """
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    friend = await session.get(User, friend_id)
    if not friend:
        raise HTTPException(status_code=404, detail="Friend not found")

    # Check if they are friends
    friendship_check = await session.execute(
        select(FriendLink).where(
            (FriendLink.user_id == current_user.id) & 
            (FriendLink.friend_id == friend_id)
        )
    )
    if not friendship_check.scalar():
        raise HTTPException(status_code=400, detail="Users are not friends")

    # Check if the friend is already invited
    existing_collaboration = await session.execute(
        select(TaskCollaborator).where(
            TaskCollaborator.task_id == task_id,
            TaskCollaborator.user_id == friend_id,
        )
    )
    if existing_collaboration.scalar():
        raise HTTPException(
            status_code=400, 
            detail="Friend is already invited to this task"
        )

    # Create the collaboration
    collaboration = TaskCollaborator(
        task_id=task_id,
        user_id=friend_id,
        invited_by_id=current_user.id,
    )
    session.add(collaboration)
    
    # Update TEAM_PLAYER level for the inviter BEFORE committing
    # +3 points for sending the invite
    await update_user_level(LevelCategory.TEAM_PLAYER, 3, current_user.id, session)
    
    # NB: The invitee (friend_id) will get points when they accept/complete
    
    await session.commit()
    await session.refresh(collaboration)
    
    return {
        "message": f"Friend {friend.username} invited to task {task.title}",
        "collaboration_id": collaboration.task_id,
        "points_awarded": 3
    }
    
@task_router.post(
    "/{task_id}/accept-invite", 
    status_code=status.HTTP_200_OK
)
async def accept_task_invite(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Accept a task collaboration invite.
    
    Points awarded:
    - +3 TEAM_PLAYER points for accepting the invite
    """
    # Check if there's a pending collaboration
    collaboration_check = await session.execute(
        select(TaskCollaborator).where(
            TaskCollaborator.task_id == task_id,
            TaskCollaborator.user_id == current_user.id,
        )
    )
    collaboration = collaboration_check.scalar()
    
    if not collaboration:
        raise HTTPException(
            status_code=404, 
            detail="No collaboration invite found for this task"
        )
    
    if collaboration.invited_by_id == current_user.id:
        raise HTTPException(
            status_code=400,
            detail="You cannot accept your own invite"
        )
    
    # Award TEAM_PLAYER points for accepting
    await update_user_level(LevelCategory.TEAM_PLAYER, 3, current_user.id, session)
    
    await session.commit()
    
    return {
        "message": "Task invite accepted",
        "task_id": task_id,
        "points_awarded": 3
    }

@task_router.get("", response_model=List[TaskSchema])
async def get_tasks(session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    result = await session.execute(select(Task).where(Task.created_by_id == current_user.id))
    tasks = result.scalars().all()
    return tasks

@task_router.get("/{task_id}", response_model=TaskSchema)
async def get_task(task_id: UUID, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.created_by_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this task")
    return task

@task_router.post("", response_model=TaskSchema)
async def create_task(
    task_data: TaskCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Create a new task and update LEADER level points.
    
    Points awarded:
    - +3 for creating a task
    - +5 for delegating (if assigned to others but not self)
    """
    try:
        # Check if workroom_id is provided and exists in the database
        if task_data.workroom_id:
            workroom = await session.get(Workroom, task_data.workroom_id)
            if not workroom:
                raise HTTPException(
                    status_code=400,
                    detail=f"Workroom with ID {task_data.workroom_id} does not exist."
                )
                
            # Check if the current user is a member of the workroom
            is_member = await session.execute(
                select(WorkroomMemberLink)
                .where(
                    WorkroomMemberLink.workroom_id == task_data.workroom_id,
                    WorkroomMemberLink.user_id == current_user.id
                )
            )
            if not is_member.scalar():
                raise HTTPException(
                    status_code=403,
                    detail="You are not a member of this workroom."
                )

        # Create new task
        new_task = Task(
            title=task_data.title,
            duration=task_data.duration,
            is_recurring=task_data.is_recurring,
            status=task_data.status,
            category=task_data.category,
            task_tools=task_data.task_tools,
            deadline=task_data.deadline,
            due_by=task_data.due_by,
            task_point=task_data.task_point,
            workroom_id=task_data.workroom_id,
            created_by_id=current_user.id
        )

        session.add(new_task)
        await session.flush()

        # Track if this is delegation (assigned to others, not self)
        is_delegation = False
        
        # Assign users to the task
        if task_data.assigned_user_ids:
            for user_id in task_data.assigned_user_ids:
                user = await session.get(User, user_id)
                if not user:
                    raise HTTPException(
                        status_code=400,
                        detail=f"User with ID {user_id} not found."
                    )
                new_task.assigned_users.append(user)
                
                # Check if delegating to someone else
                if user_id != current_user.id:
                    is_delegation = True

        # Update LEADER level BEFORE committing
        # +3 for task creation
        await update_user_level(LevelCategory.LEADER, 3, current_user.id, session)
        
        # +5 bonus for delegation (assigning to others)
        if is_delegation:
            await update_user_level(LevelCategory.LEADER, 5, current_user.id, session)

        await session.commit()
        await session.refresh(new_task)

        return new_task

    except json.JSONDecodeError as e:
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON data: {str(e)}"
        )
    except ValidationError as e:
        await session.rollback()
        raise HTTPException(
            status_code=422,
            detail=e.errors()
        )
        

@task_router.patch("/{task_id}", response_model=TaskSchema)
async def update_task(
    task_id: UUID,
    task_data: TaskUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Update a task and award WORKAHOLIC points on completion.
    
    Points awarded on completion:
    - +5 for completing task
    - +3 bonus for on-time completion
    - +2 bonus for early completion (>1 hour before deadline)
    """
    try:
        task = await session.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        if task.created_by_id != current_user.id:
            raise HTTPException(
                status_code=403, 
                detail="Not authorized to update this task"
            )

        # Check workroom permissions if workroom_id is being updated
        if task_data.workroom_id is not None:
            workroom = await session.get(Workroom, task_data.workroom_id)
            if not workroom:
                raise HTTPException(
                    status_code=400,
                    detail=f"Workroom with ID {task_data.workroom_id} does not exist."
                )
            if workroom.created_by != current_user.id:
                raise HTTPException(
                    status_code=403,
                    detail="Not authorized to add tasks to this workroom."
                )
                
        was_previously_completed = task.status == TaskStatus.COMPLETED

        # Update task fields
        update_data = task_data.dict(exclude_unset=True)
        for field, value in update_data.items():
            if field == 'assigned_user_ids':
                continue
            setattr(task, field, value)
            
        # Award XP and WORKAHOLIC points if status changed to COMPLETED
        if ('status' in update_data and 
            update_data['status'] == TaskStatus.COMPLETED and 
            not was_previously_completed):
            
            # Set completion time if not already set
            if not task.completed_at:
                task.completed_at = datetime.now(timezone.utc)
            
            # Award XP
            user = await session.get(User, task.created_by_id)
            if user:
                user.xp += task.task_point
                await session.flush()
                
            # WORKAHOLIC points breakdown:
            workaholic_points = 5  # Base completion points
            
            # Check for on-time and early completion bonuses
            if task.completed_at and task.due_by:
                if task.completed_at <= task.due_by:
                    # On-time bonus
                    workaholic_points += 3
                    
                    # Early completion bonus (>1 hour before deadline)
                    time_diff = task.due_by - task.completed_at
                    if time_diff.total_seconds() > 3600:  # More than 1 hour early
                        workaholic_points += 2
            
            # Apply all workaholic points at once
            await update_user_level(
                LevelCategory.WORKAHOLIC, 
                workaholic_points, 
                user.id, 
                session
            )

        task.updated_at = datetime.now(timezone.utc)

        # Handle assigned users if provided
        if 'assigned_user_ids' in update_data:
            new_user_ids = update_data['assigned_user_ids']
            if new_user_ids:
                users = await session.execute(
                    select(User).where(User.id.in_(new_user_ids))
                )
                users = users.scalars().all()
                if len(users) != len(new_user_ids):
                    missing = set(map(str, new_user_ids)) - {str(u.id) for u in users}
                    raise HTTPException(
                        status_code=400,
                        detail=f"Users not found: {', '.join(missing)}"
                    )
                task.assigned_users.clear()
                task.assigned_users.extend(users)

        await session.commit()
        await session.refresh(task)

        return task

    except json.JSONDecodeError as e:
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON data: {str(e)}"
        )
    except ValidationError as e:
        await session.rollback()
        raise HTTPException(
            status_code=422,
            detail=e.errors()
        )

@task_router.delete("/{task_id}")
async def delete_task(task_id: UUID, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.created_by_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this task")
    await session.delete(task)
    await session.commit()
    return {"message": "Task deleted successfully"}

