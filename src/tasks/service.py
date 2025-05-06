from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import timedelta, datetime, date
from typing import List
from uuid import UUID
from db.models import Task, TaskCollaborator, TaskStatus, User


def calculate_task_points(task: Task) -> int:
    base_points = 10
    if task.due_date and task.completed_at:
        time_diff = task.completed_at - task.due_date
        if time_diff > timedelta(0):
            if time_diff <= timedelta(hours=1):
                base_points -= 1
            elif time_diff <= timedelta(hours=6):
                base_points -= 2
            elif time_diff <= timedelta(hours=12):
                base_points -= 3
            elif time_diff <= timedelta(days=1):
                base_points -= 4
            elif time_diff <= timedelta(days=2):
                base_points -= 6
            else:
                base_points = 0
    elif task.due_date and task.completed_at is None:
        base_points = 0
    return max(0, base_points)


async def check_daily_completion(current_user: User, session: AsyncSession) -> bool:
    today = date.today()
    start_of_day = datetime.combine(today, datetime.min.time())
    end_of_day = datetime.combine(today, datetime.max.time())

    # Use SQLAlchemy's `select` instead of SQLModel's `select`
    stmt = select(Task).where(
        Task.created_by_id == current_user.id,
        Task.created_at >= start_of_day,
        Task.created_at <= end_of_day,
        Task.status != TaskStatus.COMPLETED,
    )
    result = await session.execute(stmt)
    pending_tasks = result.scalars().all()

    return not pending_tasks


async def get_friends_working_on_task(task_id: UUID, current_user: User, session: AsyncSession) -> List[UUID]:
    # Use SQLAlchemy's `select` instead of SQLModel's `select`
    stmt = select(TaskCollaborator.user_id).where(
        TaskCollaborator.task_id == task_id,
        TaskCollaborator.user_id != current_user.id,
    )
    result = await session.execute(stmt)
    friends_on_task = result.scalars().all()

    return list(friends_on_task)