from fastapi import APIRouter, HTTPException, Depends
from src.db.main import get_session
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from typing import List
from uuid import UUID
from .schema import TaskCreate, TaskUpdate
from src.db.models import Task, User, Workroom
from src.auth.dependencies import get_current_user

task_router = APIRouter()

# Task Endpoints

@task_router.get("/api/tasks", response_model=List[Task])
async def get_tasks(session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    tasks = await session.exec(select(Task).where(Task.created_by_id == current_user.id))
    tasks = tasks.all()
    return tasks

@task_router.get("/api/tasks/{task_id}", response_model=Task)
async def get_task(task_id: UUID, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.created_by_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this task")
    return task

@task_router.post("/api/tasks", response_model=Task)
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

    # Convert TaskCreate to Task and set created_by_id
    task_data_dict = task_data.model_dump()
    new_task = Task(**task_data_dict)
    new_task.created_by_id = current_user.id

    # Add the task to the session and commit
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    return new_task

@task_router.put("/api/tasks/{task_id}", response_model=Task)
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
    await session.commit()
    await session.refresh(task)

    return task

@task_router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: UUID, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.created_by_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this task")
    await session.delete(task)
    await session.flush()
    await session.commit()
    return {"message": "Task deleted successfully"}


