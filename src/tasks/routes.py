from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from datetime import datetime, date
from typing import List
from uuid import UUID
from db.db_connect import get_session
from achievements.service import update_user_streak
from .service import (calculate_task_points, check_daily_completion, 
                      get_friends_working_on_task)
from .schema import TaskCreate, TaskSchema, TaskUpdate
from db.models import (FriendLink, Task, TaskCollaborator, TaskStatus, 
                       User, Workroom, WorkroomMemberLink)
from auth.dependencies import get_current_user

task_router = APIRouter()

# Task Endpoints

@task_router.post("/{task_id}/invite-friend/{friend_id}", status_code=status.HTTP_201_CREATED)
async def invite_friend_to_task(
    task_id: UUID,
    friend_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    friend = await session.get(User, friend_id)
    if not friend:
        raise HTTPException(status_code=404, detail="Friend not found")

    # Check if they are friends
    friendship_check = await session.execute(
        select(FriendLink).where(
            (FriendLink.user_id == current_user.id) & (FriendLink.friend_id == friend_id)
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
        raise HTTPException(status_code=400, detail="Friend is already invited to this task")

    # Create the collaboration
    collaboration = TaskCollaborator(
        task_id=task_id,
        user_id=friend_id,
        invited_by_id=current_user.id,
    )
    session.add(collaboration)
    await session.commit()
    await session.refresh(collaboration)
    return {"message": f"Friend {friend.username} invited to task {task.title}"}

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

    # Convert TaskCreate to Task and set created_by_id
    task_data_dict = task_data.dict()
    new_task = Task(**task_data_dict)
    new_task.created_by_id = current_user.id

    # Add the task to the session and commit
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    return new_task

@task_router.put("/{task_id}", response_model=TaskSchema)
async def update_task(
    task_id: UUID,
    task_update: TaskUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.created_by_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to update this task")

    if task_update.workroom_id:
        workroom = await session.get(Workroom, task_update.workroom_id)
        if not workroom:
            raise HTTPException(
                status_code=400,
                detail=f"Workroom with ID {task_update.workroom_id} does not exist."
            )
        if workroom.created_by != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="Not authorized to add tasks to this workroom."
            )

    for key, value in task_update.dict(exclude_unset=True).items():
        setattr(task, key, value)

    if task.status == TaskStatus.COMPLETED and task_update.status != TaskStatus.COMPLETED:
        task.completed_at = datetime.utcnow()
        points = calculate_task_points(task)
        current_user.xp += points

        # Friend Invitation Points
        friends_working = await get_friends_working_on_task(task_id, current_user.id, session)
        for friend_id in friends_working:
            friend = await session.get(User, friend_id)
            if friend:
                friend.xp += 5
                session.add(friend)

        # Daily Task Completion Bonus
        if await check_daily_completion(current_user.id, session):
            today_tasks = await session.execute(select(Task).where(Task.created_by_id == current_user.id, and_(Task.created_at >= datetime.combine(date.today(), datetime.min.time()), Task.created_at <= datetime.combine(date.today(), datetime.max.time()), Task.status == TaskStatus.COMPLETED)))
            current_user.xp += (len(today_tasks.scalars().all()) * 2) + 10

        session.add(current_user)
        
        # Update User Streak
        await update_user_streak(current_user.id, session)

    await session.commit()
    await session.refresh(task)
    await session.refresh(current_user)

    return task

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