from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from typing import Dict, Any
from app_src.achievements.service import update_dashboard_stats
from app_src.db.db_connect import get_session
from app_src.auth.dependencies import get_current_user
from app_src.db.models import User, FriendLink, UserStreak, Task, UserLevel, task_assignees
from datetime import date, datetime
import cachetools.func

dashboard_router = APIRouter()

async def _get_user_friends(session: AsyncSession, user_id: str):
    """Fetches a user's friends (limit 3 for dashboard)"""
    result_friends = await session.execute(
        select(User)
        .join(FriendLink, (User.id == FriendLink.friend_id))
        .where(FriendLink.user_id == user_id)
        .limit(3)
    )
    friends = [
        {"id": friend.id, "username": friend.username, "avatar_url": friend.avatar_url}
        for friend in result_friends.scalars().all()
    ]
    return friends

async def _get_user_levels(session: AsyncSession, user_id: str):
    """Fetches user levels across all categories"""
    result_levels = await session.execute(
        select(UserLevel).where(UserLevel.user_id == user_id)
    )
    levels = [
        {
            "category": level.level_category,
            "tier": level.level_tier,
            "points": level.level_points
        }
        for level in result_levels.scalars().all()
    ]
    return levels

async def _get_daily_tasks(session: AsyncSession, user_id: str, start_of_day: datetime, end_of_day: datetime):
    """
    Fetches daily tasks for the user.
    
    Includes:
    - Tasks created by user with no assignees (personal tasks)
    - Tasks created by user where they are also an assignee
    - Tasks assigned to user (even if created by others)
    
    Excludes:
    - Tasks created by user but assigned ONLY to others
    """
    # Subquery to check if user is an assignee
    user_assignee_subquery = (
        select(task_assignees.c.task_id)
        .where(task_assignees.c.user_id == user_id)
    )
    
    result_tasks = await session.execute(
        select(Task)
        .outerjoin(task_assignees, Task.id == task_assignees.c.task_id)
        .where(
            Task.created_at >= start_of_day,
            Task.created_at <= end_of_day,
            or_(
                # Tasks created by user with no assignees
                and_(
                    Task.created_by_id == user_id,
                    ~Task.id.in_(select(task_assignees.c.task_id))
                ),
                # Tasks where user is creator AND assignee
                and_(
                    Task.created_by_id == user_id,
                    Task.id.in_(user_assignee_subquery)
                ),
                # Tasks assigned to user (regardless of creator)
                and_(
                    Task.created_by_id != user_id,
                    Task.id.in_(user_assignee_subquery)
                )
            )
        )
        .distinct()
    )
    
    daily_tasks = [
        {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "due_by": task.due_by.isoformat() if task.due_by else None,
            "category": task.category
        }
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
    Returns comprehensive dashboard data with real-time calculated stats.
    
    Includes:
    - User profile information
    - Productivity percentage (calculated)
    - Average task completion time (calculated)
    - Current streak
    - Level progress across categories
    - Today's tasks
    - Friend list
    """
    user_id = current_user.id
    
    # Update all dashboard stats (streak, productivity, levels, etc.)
    await update_dashboard_stats(user_id, session)
    
    # Refresh user to get updated values
    await session.refresh(current_user)

    # Build user data response
    user_data = {
        "username": current_user.username,
        "first_name": current_user.first_name,
        "last_name": current_user.last_name,
        "email": current_user.email,
        "xp": current_user.xp,
        "level": current_user.level,
        "avatar_url": current_user.avatar_url,
        "productivity_percentage": float(current_user.productivity),
        "average_task_time_hours": float(current_user.average_task_time),
        "teamwork_collaborations": current_user.teamwork_collaborations,
        "daily_active_minutes": current_user.daily_active_minutes,
    }

    # Fetch current streak
    result_streak = await session.execute(
        select(UserStreak).where(UserStreak.user_id == user_id)
    )
    user_streak = result_streak.scalars().first()
    user_data["streaks"] = {
        "current": user_streak.current_streak if user_streak else 1,
        "highest": user_streak.highest_streak if user_streak else 1,
        "last_active": user_streak.last_active_date.isoformat() if user_streak and user_streak.last_active_date else None
    }

    # Fetch friends
    friends = await _get_user_friends(session, user_id)
    user_data["friends"] = friends

    # Fetch today's tasks
    today = date.today()
    start_of_day = datetime(today.year, today.month, today.day, 0, 0, 0)
    end_of_day = datetime(today.year, today.month, today.day, 23, 59, 59)
    daily_tasks = await _get_daily_tasks(session, user_id, start_of_day, end_of_day)
    user_data["daily_tasks"] = daily_tasks

    # Fetch user levels with progress information
    levels = await _get_user_levels(session, user_id)
    
    # Add progress to next tier for each level
    for level in levels:
        points = level["points"]
        tier = level["tier"]
        
        # Calculate points needed for next tier
        if tier == "Beginner":
            next_tier_threshold = 100
            progress = (points / next_tier_threshold) * 100
        elif tier == "Intermediate":
            next_tier_threshold = 300
            progress = (points / next_tier_threshold) * 100
        elif tier == "Advanced":
            next_tier_threshold = 600
            progress = (points / next_tier_threshold) * 100
        else:  # Expert
            progress = 100
            next_tier_threshold = None
        
        level["progress"] = round(progress, 2)
        level["next_tier_points"] = next_tier_threshold
    
    user_data["levels"] = levels
    
    # Cache for 10 seconds to prevent excessive recalculation
    response.headers["Cache-Control"] = "max-age=10"

    return user_data
