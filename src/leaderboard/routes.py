from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import select
from src.db.main import get_session
from sqlmodel.ext.asyncio.session import AsyncSession
from typing import List
from uuid import UUID
from src.db.models import Leaderboard, User
from src.auth.dependencies import get_current_user

leaderboard_router = APIRouter()

# Leaderboard Endpoints

@leaderboard_router.get("/api/leaderboard", response_model=List[Leaderboard])
def get_leaderboard(session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    leaderboard = session.exec(select(Leaderboard).where(Leaderboard.user_id == current_user.id)).all()
    return leaderboard

@leaderboard_router.get("/api/leaderboard/{leaderboard_id}", response_model=Leaderboard)
def get_leaderboard_entry(leaderboard_id: UUID, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    leaderboard_entry = session.get(Leaderboard, leaderboard_id)
    if not leaderboard_entry:
        raise HTTPException(status_code=404, detail="Leaderboard entry not found")
    if leaderboard_entry.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this leaderboard entry")
    return leaderboard_entry

@leaderboard_router.post("/api/leaderboard", response_model=Leaderboard)
def create_leaderboard_entry(leaderboard: Leaderboard, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    leaderboard.user_id = current_user.id
    session.add(leaderboard)
    session.commit()
    session.refresh(leaderboard)
    return leaderboard

@leaderboard_router.put("/api/leaderboard/{leaderboard_id}", response_model=Leaderboard)
def update_leaderboard_entry(leaderboard_id: UUID, leaderboard_update: Leaderboard, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    leaderboard_entry = session.get(Leaderboard, leaderboard_id)
    if not leaderboard_entry:
        raise HTTPException(status_code=404, detail="Leaderboard entry not found")
    if leaderboard_entry.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to update this leaderboard entry")
    for key, value in leaderboard_update.dict().items():
        setattr(leaderboard_entry, key, value)
    session.commit()
    session.refresh(leaderboard_entry)
    return leaderboard_entry

@leaderboard_router.delete("/api/leaderboard/{leaderboard_id}")
def delete_leaderboard_entry(leaderboard_id: UUID, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    leaderboard_entry = session.get(Leaderboard, leaderboard_id)
    if not leaderboard_entry:
        raise HTTPException(status_code=404, detail="Leaderboard entry not found")
    if leaderboard_entry.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this leaderboard entry")
    session.delete(leaderboard_entry)
    session.commit()
    return {"message": "Leaderboard entry deleted successfully"}