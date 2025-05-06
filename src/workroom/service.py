from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from db.models import TaskStatus, Workroom, Leaderboard, TaskCollaborator, Task
from tasks.service import calculate_task_points


async def update_workroom_leaderboard(workroom_id: UUID, session: AsyncSession):
    # Fetch the workroom
    workroom = await session.get(Workroom, workroom_id)
    if not workroom:
        raise HTTPException(status_code=404, detail="Workroom not found")

    leaderboard_data = []

    # Iterate through each member in the workroom
    for member in workroom.members:
        # Calculate total XP within the workroom
        total_xp = 0
        workroom_tasks = await session.execute(
            select(Task).where(
                Task.workroom_id == workroom_id,
                Task.created_by_id == member.id,
                Task.status == TaskStatus.COMPLETED
            )
        )
        workroom_tasks = workroom_tasks.scalars().all()
        for task in workroom_tasks:
            total_xp += calculate_task_points(task)

        # Calculate total teamwork points within the workroom
        teamwork_points = 0
        collaborations = await session.execute(
            select(TaskCollaborator)
            .join(Task)
            .where(
                TaskCollaborator.user_id == member.id,
                Task.workroom_id == workroom_id
            )
        )
        collaborations = collaborations.scalars().all()
        for collaboration in collaborations:
            task = await session.get(Task, collaboration.task_id)
            if task.status == TaskStatus.COMPLETED:
                teamwork_points += 5  # 5 points per collaboration.

        leaderboard_data.append({
            "user_id": member.id,
            "username": member.username,
            "score": total_xp,
            "teamwork_score": teamwork_points,
        })

    # Sort the leaderboard data
    leaderboard_data.sort(key=lambda x: (-x["score"], -x["teamwork_score"], x["username"]))

    # Update or create leaderboard entries
    for rank, entry in enumerate(leaderboard_data, start=1):
        leaderboard_entry = await session.execute(
            select(Leaderboard).where(
                Leaderboard.workroom_id == workroom_id,
                Leaderboard.user_id == entry["user_id"]
            )
        )
        leaderboard_entry = leaderboard_entry.scalar_one_or_none()

        if leaderboard_entry:
            leaderboard_entry.score = entry["score"]
            leaderboard_entry.teamwork_score = entry["teamwork_score"]
            leaderboard_entry.rank = rank
        else:
            new_leaderboard_entry = Leaderboard(
                workroom_id=workroom_id,
                user_id=entry["user_id"],
                score=entry["score"],
                teamwork_score=entry["teamwork_score"],
                rank=rank,
            )
            session.add(new_leaderboard_entry)

    await session.commit()