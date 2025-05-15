from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from db.models import (LevelCategory, LevelTier, Task, TaskCollaborator, 
                       TaskStatus, UserLevel, UserStreak)
from datetime import date, timedelta


def determine_level_tier(points: int) -> LevelTier:
    if points < 50:
        return LevelTier.BEGINNER
    elif points < 150:
        return LevelTier.INTERMEDIATE
    elif points < 300:
        return LevelTier.ADVANCED
    else:
        return LevelTier.EXPERT


async def get_or_create_user_level(category: LevelCategory, user_id, session: AsyncSession) -> UserLevel:
    user_level = await session.execute(select(UserLevel).where(UserLevel.user_id == user_id, UserLevel.level_category == category))
    user_level = user_level.scalar()
    if not user_level:
        user_level = UserLevel(user_id=user_id, level_category=category, level_tier=LevelTier.BEGINNER)
        session.add(user_level)
        await session.commit()
        await session.refresh(user_level)
    return user_level

async def update_user_level(category: LevelCategory, points: int, user_id, session: AsyncSession):
    user_level = await get_or_create_user_level(category, user_id, session)
    user_level.level_points += points
    user_level.level_tier = determine_level_tier(user_level.level_points)
    await session.commit()
    await session.refresh(user_level)

# --- Activity Tracking and Point Calculation ---

async def calculate_leader_points(user_id, session: AsyncSession):
    # Task creation: 5 points per task.
    tasks_created = await session.execute(select(Task).where(Task.created_by_id == user_id))
    task_creation_points = len(tasks_created.scalars().all()) * 5

    # Task delegation: 10 points per successful delegation. (Needs more complex logic)
    # Delegation logic is not implemented.
    delegation_points = 0

    return task_creation_points + delegation_points

async def calculate_workaholic_points(user_id, session: AsyncSession):
    # Task completion: 3 points per task.
    tasks_completed = await session.execute(select(Task).where(Task.created_by_id == user_id, Task.status == TaskStatus.COMPLETED))
    task_completion_points = len(tasks_completed.scalars().all()) * 3

    # On-time completion: 2 bonus points. (Needs completed_at and due_date)
    on_time_completion_points = 0
    tasks = tasks_completed.scalars().all()
    for task in tasks:
        if task.due_date and task.completed_at and task.completed_at <= task.due_date:
            on_time_completion_points += 2

    return task_completion_points + on_time_completion_points

async def calculate_team_player_points(user_id, session: AsyncSession):
    # Task collaboration: 5 points per collaboration.
    collaborations = await session.execute(select(TaskCollaborator).where(TaskCollaborator.user_id == user_id))
    collaboration_points = len(collaborations.scalars().all()) * 5

    # Accepting collaboration invite: 3 points.
    invites_accepted = await session.execute(select(TaskCollaborator).where(TaskCollaborator.user_id == user_id, TaskCollaborator.invited_by_id != user_id))
    invite_points = len(invites_accepted.scalars().all()) * 3

    return collaboration_points + invite_points

async def calculate_slacker_points(user_id, session: AsyncSession):
    # Task completion below 20%: -5 points.
    tasks_created = await session.execute(select(Task).where(Task.created_by_id == user_id))
    tasks_completed = await session.execute(select(Task).where(Task.created_by_id == user_id, Task.status == TaskStatus.COMPLETED))

    created_count = len(tasks_created.scalars().all())
    completed_count = len(tasks_completed.scalars().all())
    if created_count > 0 and (completed_count / created_count) < 0.2:
        task_completion_points = -5
    else:
        task_completion_points = 0
    # Daily active time below 1 hour: -3 points. (Needs activity tracking)
    # Activity tracking is not implemented.
    active_time_points = 0

    return task_completion_points + active_time_points

# --- Level Update Logic ---

async def update_user_levels(user_id, session: AsyncSession):
    leader_points = await calculate_leader_points(user_id, session)
    workaholic_points = await calculate_workaholic_points(user_id, session)
    team_player_points = await calculate_team_player_points(user_id, session)
    slacker_points = await calculate_slacker_points(user_id, session)

    await update_user_level(LevelCategory.LEADER, leader_points, user_id, session)
    await update_user_level(LevelCategory.WORKAHOLIC, workaholic_points, user_id, session)
    await update_user_level(LevelCategory.TEAM_PLAYER, team_player_points, user_id, session)
    await update_user_level(LevelCategory.SLACKER, slacker_points, user_id, session)


async def update_user_streak(user_id, session: AsyncSession):
    today = date.today()
    user_streak = await session.execute(select(UserStreak).where(UserStreak.user_id == user_id))
    user_streak = user_streak.scalar()
    if not user_streak:
        user_streak = UserStreak(user_id=user_id)
        session.add(user_streak)
        await session.commit()
        await session.refresh(user_streak)

    if user_streak.last_active_date == today:
        return  # Already updated today

    if user_streak.last_active_date == today - timedelta(days=1):
        user_streak.current_streak += 1
    else:
        user_streak.current_streak = 1

    user_streak.last_active_date = today

    if user_streak.current_streak > user_streak.highest_streak:
        user_streak.highest_streak = user_streak.current_streak

    await session.commit()
    
