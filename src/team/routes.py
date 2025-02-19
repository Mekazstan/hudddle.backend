from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import select
from src.db.main import get_session
from sqlmodel.ext.asyncio.session import AsyncSession
from typing import List
from uuid import UUID
from src.db.models import Team, User
from src.auth.dependencies import get_current_user

team_router = APIRouter()

# Team Endpoints

@team_router.get("/api/teams", response_model=List[Team])
def get_teams(session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    teams = session.exec(select(Team).where(Team.created_by == current_user.id)).all()
    return teams

@team_router.get("/api/teams/{team_id}", response_model=Team)
def get_team(team_id: UUID, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    team = session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    if team.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this team")
    return team

@team_router.post("/api/teams", response_model=Team)
def create_team(team: Team, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    team.created_by = current_user.id
    session.add(team)
    session.commit()
    session.refresh(team)
    return team

@team_router.put("/api/teams/{team_id}", response_model=Team)
def update_team(team_id: UUID, team_update: Team, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    team = session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    if team.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to update this team")
    for key, value in team_update.dict().items():
        setattr(team, key, value)
    session.commit()
    session.refresh(team)
    return team

@team_router.delete("/api/teams/{team_id}")
def delete_team(team_id: UUID, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    team = session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    if team.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this team")
    session.delete(team)
    session.commit()
    return {"message": "Team deleted successfully"}