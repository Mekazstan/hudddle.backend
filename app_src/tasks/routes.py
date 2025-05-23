import json
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone
from typing import List
from uuid import UUID
from app_src.db.db_connect import get_session
from .schema import TaskCreate, TaskSchema, TaskUpdate
from app_src.db.models import (FriendLink, Task, TaskCollaborator,
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

        await session.commit()
        await session.refresh(new_task)

        return new_task

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON data: {str(e)}"
        )
    except ValidationError as e:
        raise HTTPException(
            status_code=422,
            detail=e.errors()
        )

@task_router.put("/{task_id}", response_model=TaskSchema)
async def update_task(
    task_id: UUID,
    task_data: TaskUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    try:
        task = await session.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        if task.created_by_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized to update this task")

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

        # Update task fields
        update_data = task_data.dict(exclude_unset=True)
        for field, value in update_data.items():
            if field == 'assigned_user_ids':
                # Handle user assignments separately
                continue
            setattr(task, field, value)

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

        task.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(task)

        return task

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON data: {str(e)}"
        )
    except ValidationError as e:
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