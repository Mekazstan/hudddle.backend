from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date
from typing import List, Dict, Any
from app_src.db.models import UserLevel, UserStreak, User
from app_src.db.db_connect import get_session
from app_src.auth.dependencies import get_current_user
from .service import update_user_levels


achievement_router = APIRouter()

@achievement_router.get("/users/me/levels", response_model=List[Dict[str, Any]])
async def get_user_levels(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await update_user_levels(current_user.id, session)  # Update levels before returning them.
    result = await session.execute(select(UserLevel).where(UserLevel.user_id == current_user.id))
    user_levels = result.scalars().all()
    return [
        {
            "category": level.level_category,
            "tier": level.level_tier,
            "points": level.level_points,
        }
        for level in user_levels
    ]


@achievement_router.get("/levels", response_model=List[Dict[str, Any]])
async def get_all_user_levels(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(UserLevel))
    user_levels = result.scalars().all()
    return [
        {
            "user_id": level.user_id,
            "category": level.level_category,
            "tier": level.level_tier,
            "points": level.level_points,
        }
        for level in user_levels
    ]


@achievement_router.get("/users/me/streak", response_model=Dict[str, Any])
async def get_user_streak(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(UserStreak).where(UserStreak.user_id == user.id))
    user_streak = result.scalars().first()
    today = date.today()
    if not user_streak:
        return {"current_streak": 1, "highest_streak": 1, "last_active_date": today}
    return {
        "current_streak": user_streak.current_streak,
        "highest_streak": user_streak.highest_streak,
        "last_active_date": user_streak.last_active_date,
    }
    
