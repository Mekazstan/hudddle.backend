from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from app_src.db.models import (LevelCategory, LevelTier, Task, TaskCollaborator, 
                       TaskStatus, User, UserLevel, UserStreak, task_assignees)
from datetime import date, datetime, timedelta


def determine_level_tier(points: int) -> LevelTier:
    """Determines level tier based on accumulated points"""
    if points < 100:
        return LevelTier.BEGINNER
    elif points < 300:
        return LevelTier.INTERMEDIATE
    elif points < 600:
        return LevelTier.ADVANCED
    else:
        return LevelTier.EXPERT


async def get_or_create_user_level(
    category: LevelCategory, 
    user_id, 
    session: AsyncSession
) -> UserLevel:
    """Get or create a user level for a specific category"""
    result = await session.execute(
        select(UserLevel)
        .where(
            UserLevel.user_id == user_id,
            UserLevel.level_category == category
        )
    )
    user_level = result.scalar()
    
    if not user_level:
        user_level = UserLevel(
            user_id=user_id,
            level_category=category,
            level_tier=LevelTier.BEGINNER,
            level_points=0
        )
        session.add(user_level)
        await session.flush()
    
    return user_level

async def update_user_level(
    category: LevelCategory,
    points: int,
    user_id,
    session: AsyncSession
) -> UserLevel:
    """Update user level by adding points"""
    user_level = await get_or_create_user_level(category, user_id, session)
    user_level.level_points = max(0, user_level.level_points + points)
    user_level.level_tier = determine_level_tier(user_level.level_points)
    await session.flush()
    return user_level

async def recalculate_all_user_levels(user_id, session: AsyncSession):
    """
    Recalculates all user levels from scratch based on current data.
    Call this periodically or after significant changes.
    """
    leader_points = await calculate_leader_points(user_id, session)
    workaholic_points = await calculate_workaholic_points(user_id, session)
    team_player_points = await calculate_team_player_points(user_id, session)
    slacker_points = await calculate_slacker_points(user_id, session)
    
    # Update each level
    for category, points in [
        (LevelCategory.LEADER, leader_points),
        (LevelCategory.WORKAHOLIC, workaholic_points),
        (LevelCategory.TEAM_PLAYER, team_player_points),
        (LevelCategory.SLACKER, slacker_points),
    ]:
        user_level = await get_or_create_user_level(category, user_id, session)
        user_level.level_points = points
        user_level.level_tier = determine_level_tier(points)
    
    await session.commit()

# --- Activity Tracking and Point Calculation ---

async def calculate_leader_points(user_id, session: AsyncSession) -> int:
    """
    Leader points based on:
    - Creating tasks: 3 points each
    - Tasks assigned to others (delegation): 5 points each
    - Workroom creation: 10 points each
    """
    # Task creation
    tasks_created = await session.execute(
        select(func.count(Task.id))
        .where(Task.created_by_id == user_id)
    )
    task_points = (tasks_created.scalar() or 0) * 3
    
    # Delegation (tasks assigned to others, not self)
    delegated_tasks = await session.execute(
        select(func.count(Task.id.distinct()))
        .select_from(Task)
        .join(task_assignees, Task.id == task_assignees.c.task_id)
        .where(
            Task.created_by_id == user_id,
            task_assignees.c.user_id != user_id
        )
    )
    delegation_points = (delegated_tasks.scalar() or 0) * 5
    
    return task_points + delegation_points


async def calculate_workaholic_points(user_id, session: AsyncSession) -> int:
    """
    Workaholic points based on:
    - Completing tasks: 5 points each
    - On-time completion: 3 bonus points
    - Completing tasks early (before due date): 2 bonus points
    """
    # Get all completed tasks
    completed_tasks_query = await session.execute(
        select(Task)
        .where(
            or_(
                Task.created_by_id == user_id,
                Task.id.in_(
                    select(task_assignees.c.task_id)
                    .where(task_assignees.c.user_id == user_id)
                )
            ),
            Task.status == TaskStatus.COMPLETED
        )
    )
    completed_tasks = completed_tasks_query.scalars().all()
    
    base_points = len(completed_tasks) * 5
    bonus_points = 0
    
    for task in completed_tasks:
        if task.due_by and task.completed_at:
            if task.completed_at <= task.due_by:
                bonus_points += 3  # On-time bonus
                
                # Early completion bonus (completed more than 1 hour early)
                time_diff = task.due_by - task.completed_at
                if time_diff.total_seconds() > 3600:
                    bonus_points += 2
    
    return base_points + bonus_points


async def calculate_team_player_points(user_id, session: AsyncSession) -> int:
    """
    Team Player points based on:
    - Working on collaborative tasks: 4 points each
    - Being invited to tasks: 3 points per invite
    - Completing collaborative tasks: 5 bonus points
    """
    # Collaborative tasks (where user is a collaborator)
    collaborations = await session.execute(
        select(func.count(TaskCollaborator.task_id))
        .where(TaskCollaborator.user_id == user_id)
    )
    collab_points = (collaborations.scalar() or 0) * 4
    
    # Invites accepted (invited by someone else)
    invites = await session.execute(
        select(func.count(TaskCollaborator.task_id))
        .where(
            TaskCollaborator.user_id == user_id,
            TaskCollaborator.invited_by_id.isnot(None),
            TaskCollaborator.invited_by_id != user_id
        )
    )
    invite_points = (invites.scalar() or 0) * 3
    
    # Completed collaborative tasks bonus
    completed_collab = await session.execute(
        select(func.count(Task.id.distinct()))
        .select_from(Task)
        .join(TaskCollaborator, Task.id == TaskCollaborator.task_id)
        .where(
            TaskCollaborator.user_id == user_id,
            Task.status == TaskStatus.COMPLETED
        )
    )
    completed_bonus = (completed_collab.scalar() or 0) * 5
    
    return collab_points + invite_points + completed_bonus


async def calculate_slacker_points(user_id, session: AsyncSession) -> int:
    """
    Slacker points (higher is worse):
    - Task completion below 50%: +5 points
    - Overdue tasks: +3 points each
    - Broken streak: +10 points
    
    Note: Deduct points for good behavior (negative slacker score)
    """
    # Check completion rate
    total_tasks = await session.execute(
        select(func.count(Task.id))
        .where(
            or_(
                Task.created_by_id == user_id,
                Task.id.in_(
                    select(task_assignees.c.task_id)
                    .where(task_assignees.c.user_id == user_id)
                )
            )
        )
    )
    total_count = total_tasks.scalar() or 0
    
    completed_tasks = await session.execute(
        select(func.count(Task.id))
        .where(
            or_(
                Task.created_by_id == user_id,
                Task.id.in_(
                    select(task_assignees.c.task_id)
                    .where(task_assignees.c.user_id == user_id)
                )
            ),
            Task.status == TaskStatus.COMPLETED
        )
    )
    completed_count = completed_tasks.scalar() or 0
    
    points = 0
    
    # Low completion rate penalty
    if total_count > 0:
        completion_rate = completed_count / total_count
        if completion_rate < 0.5:
            points += 5
    
    # Overdue tasks penalty
    overdue_tasks = await session.execute(
        select(func.count(Task.id))
        .where(
            or_(
                Task.created_by_id == user_id,
                Task.id.in_(
                    select(task_assignees.c.task_id)
                    .where(task_assignees.c.user_id == user_id)
                )
            ),
            Task.status == TaskStatus.OVERDUE
        )
    )
    overdue_count = overdue_tasks.scalar() or 0
    points += overdue_count * 3
    
    # Check streak (broken streak already handled in update_user_streak)
    streak_result = await session.execute(
        select(UserStreak.current_streak)
        .where(UserStreak.user_id == user_id)
    )
    current_streak = streak_result.scalar() or 1
    
    # Reward good streaks with negative slacker points
    if current_streak >= 7:
        points -= 5
    if current_streak >= 30:
        points -= 10
    
    return points

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
    """Updates user streak and applies slacker points"""
    today = date.today()
    
    result = await session.execute(
        select(UserStreak).where(UserStreak.user_id == user_id)
    )
    user_streak = result.scalar()
    
    if not user_streak:
        user_streak = UserStreak(
            user_id=user_id,
            current_streak=1,
            last_active_date=today,
            highest_streak=1
        )
        session.add(user_streak)
        await session.flush()
        return
    
    # Already updated today
    if user_streak.last_active_date == today:
        return
    
    # Check if streak continues
    if user_streak.last_active_date == today - timedelta(days=1):
        # Continue streak
        user_streak.current_streak += 1
        await update_user_level(LevelCategory.SLACKER, -2, user_id, session)
    else:
        # Streak broken
        user_streak.current_streak = 1
        await update_user_level(LevelCategory.SLACKER, 10, user_id, session)
    
    user_streak.last_active_date = today
    
    # Update highest streak
    if user_streak.current_streak > user_streak.highest_streak:
        user_streak.highest_streak = user_streak.current_streak
    
    await session.flush()

async def calculate_productivity_percentage(user_id, session: AsyncSession) -> float:
    """
    Calculates productivity as a weighted score based on:
    - Task completion rate (40%)
    - On-time completion rate (30%)
    - Active streak (20%)
    - Daily task completion (10%)
    
    Returns a percentage between 0-100
    """
    
    # 1. Task Completion Rate (40% weight)
    total_tasks = await session.execute(
        select(func.count(Task.id))
        .where(
            or_(
                Task.created_by_id == user_id,
                Task.id.in_(
                    select(task_assignees.c.task_id)
                    .where(task_assignees.c.user_id == user_id)
                )
            )
        )
    )
    total_count = total_tasks.scalar() or 0
    
    completed_tasks = await session.execute(
        select(func.count(Task.id))
        .where(
            or_(
                Task.created_by_id == user_id,
                Task.id.in_(
                    select(task_assignees.c.task_id)
                    .where(task_assignees.c.user_id == user_id)
                )
            ),
            Task.status == TaskStatus.COMPLETED
        )
    )
    completed_count = completed_tasks.scalar() or 0
    
    completion_rate = (completed_count / total_count * 100) if total_count > 0 else 0
    completion_score = completion_rate * 0.40
    
    # 2. On-time Completion Rate (30% weight)
    on_time_tasks = await session.execute(
        select(func.count(Task.id))
        .where(
            or_(
                Task.created_by_id == user_id,
                Task.id.in_(
                    select(task_assignees.c.task_id)
                    .where(task_assignees.c.user_id == user_id)
                )
            ),
            Task.status == TaskStatus.COMPLETED,
            Task.completed_at.isnot(None),
            Task.due_by.isnot(None),
            Task.completed_at <= Task.due_by
        )
    )
    on_time_count = on_time_tasks.scalar() or 0
    on_time_rate = (on_time_count / completed_count * 100) if completed_count > 0 else 0
    on_time_score = on_time_rate * 0.30
    
    # 3. Streak Bonus (20% weight) - normalize to 100
    streak_result = await session.execute(
        select(UserStreak.current_streak)
        .where(UserStreak.user_id == user_id)
    )
    current_streak = streak_result.scalar() or 1
    # Cap streak bonus at 30 days for realistic scoring
    streak_normalized = min(current_streak / 30 * 100, 100)
    streak_score = streak_normalized * 0.20
    
    # 4. Daily Completion Bonus (10% weight)
    today = date.today()
    start_of_day = datetime.combine(today, datetime.min.time())
    end_of_day = datetime.combine(today, datetime.max.time())
    
    daily_total = await session.execute(
        select(func.count(Task.id))
        .where(
            or_(
                Task.created_by_id == user_id,
                Task.id.in_(
                    select(task_assignees.c.task_id)
                    .where(task_assignees.c.user_id == user_id)
                )
            ),
            Task.created_at >= start_of_day,
            Task.created_at <= end_of_day
        )
    )
    daily_total_count = daily_total.scalar() or 0
    
    daily_completed = await session.execute(
        select(func.count(Task.id))
        .where(
            or_(
                Task.created_by_id == user_id,
                Task.id.in_(
                    select(task_assignees.c.task_id)
                    .where(task_assignees.c.user_id == user_id)
                )
            ),
            Task.created_at >= start_of_day,
            Task.created_at <= end_of_day,
            Task.status == TaskStatus.COMPLETED
        )
    )
    daily_completed_count = daily_completed.scalar() or 0
    daily_rate = (daily_completed_count / daily_total_count * 100) if daily_total_count > 0 else 100
    daily_score = daily_rate * 0.10
    
    # Total productivity score
    productivity = completion_score + on_time_score + streak_score + daily_score
    return round(min(productivity, 100), 2)

async def calculate_average_task_time(user_id, session: AsyncSession) -> float:
    """
    Calculates average time to complete tasks in hours.
    Only considers completed tasks with both created_at and completed_at.
    
    Returns average in hours (e.g., 2.5 = 2 hours 30 minutes)
    """
    completed_tasks_query = await session.execute(
        select(Task.created_at, Task.completed_at)
        .where(
            or_(
                Task.created_by_id == user_id,
                Task.id.in_(
                    select(task_assignees.c.task_id)
                    .where(task_assignees.c.user_id == user_id)
                )
            ),
            Task.status == TaskStatus.COMPLETED,
            Task.completed_at.isnot(None)
        )
    )
    
    completed_tasks = completed_tasks_query.all()
    
    if not completed_tasks:
        return 0.0
    
    total_hours = 0.0
    for task in completed_tasks:
        time_diff = task.completed_at - task.created_at
        hours = time_diff.total_seconds() / 3600
        total_hours += hours
    
    average_hours = total_hours / len(completed_tasks)
    return round(average_hours, 2)

async def update_dashboard_stats(user_id, session: AsyncSession):
    """
    Updates all calculated stats for the user.
    Call this when dashboard is accessed or periodically.
    """
    # Get user
    user_result = await session.execute(
        select(User).where(User.id == user_id)
    )
    user = user_result.scalar()
    
    if not user:
        return
    
    # Update productivity
    user.productivity = await calculate_productivity_percentage(user_id, session)
    
    # Update average task time
    user.average_task_time = await calculate_average_task_time(user_id, session)
    
    # Update streak
    await update_user_streak(user_id, session)
    
    # Recalculate levels (do this less frequently in production)
    await recalculate_all_user_levels(user_id, session)
    
    await session.commit()
