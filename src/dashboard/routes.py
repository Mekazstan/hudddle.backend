from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Dict, Any
from db.db_connect import get_session
from auth.dependencies import get_current_user
from db.models import User, FriendLink, UserStreak, Task, UserLevel
from datetime import date, datetime
import cachetools.func

dashboard_router = APIRouter()

@cachetools.func.ttl_cache(maxsize=128, ttl=300)  # Cache for 5 minutes
async def _get_user_friends(session: AsyncSession, user_id: str):
    """
    Fetches and caches a user's friends for 5 minutes.
    """
    result_friends = await session.execute(
        select(User)
        .join(FriendLink, (User.id == FriendLink.friend_id))
        .where(FriendLink.user_id == user_id)
        .limit(3)
    )
    friends = [
        {"id": friend.id, "username": friend.username}
        for friend in result_friends.scalars().all()
    ]
    return friends

@cachetools.func.ttl_cache(maxsize=128, ttl=3600)  # Cache for 1 hour
async def _get_user_levels(session: AsyncSession, user_id: str):
    """
    Fetches and caches user levels for 1 hour.
    """
    result_levels = await session.execute(
        select(UserLevel).where(UserLevel.user_id == user_id)
    )
    levels = [
        {"category": level.level_category, "tier": level.level_tier}
        for level in result_levels.scalars().all()
    ]
    return levels

async def _get_daily_tasks(session: AsyncSession, user_id: str, start_of_day: datetime, end_of_day: datetime):
    """
    Fetches daily tasks
    """
    result_tasks = await session.execute(
        select(Task)
        .where(Task.created_by_id == user_id)
        .where(Task.created_at >= start_of_day)
        .where(Task.created_at <= end_of_day)
    )
    
    daily_tasks = [
        {"id": task.id, "title": task.title, "status": task.status}
        for task in result_tasks.scalars().all()
    ]
    return daily_tasks

@dashboard_router.get("", response_model=Dict[str, Any])
async def get_dashboard_data(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    response: Response = Response(),
):
    """
    Returns a comprehensive dashboard of user-related data.
    """
    user_id = current_user.id

    # Fetch basic user information
    user_data = {
        "username": current_user.username,
        "first_name": current_user.first_name,
        "last_name": current_user.last_name,
        "email": current_user.email,
        "xp": current_user.xp,
        "productivity_percentage": float(current_user.productivity),
        "average_task_time_hours": float(current_user.average_task_time),
        "teamwork_collaborations": current_user.teamwork_collaborations,
        "avatar_url": current_user.avatar_url
    }

    # Fetch streaks
    result_streak = await session.execute(
        select(UserStreak).where(UserStreak.user_id == user_id)
    )
    user_streak = result_streak.scalars().first()
    user_data["streaks"] = user_streak.current_streak if user_streak else 0

    # Fetch 3 friends
    friends = await _get_user_friends(session, user_id)
    user_data["friends"] = friends

    # Fetch daily tasks (assuming tasks created by the user today)
    today = date.today()
    start_of_day = datetime(today.year, today.month, today.day, 0, 0, 0)
    end_of_day = datetime(today.year, today.month, today.day, 23, 59, 59)
    daily_tasks = await _get_daily_tasks(session, user_id, start_of_day, end_of_day)
    user_data["daily_tasks"] = daily_tasks

    # Fetch user levels
    levels = await _get_user_levels(session, user_id)
    user_data["levels"] = levels
    
    response.headers["Cache-Control"] = "max-age=10"

    # Expects 'daily_active_minutes' to be updated regularly from the frontend
    user_data["daily_active_minutes"] = current_user.daily_active_minutes

    return user_data

