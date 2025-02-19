from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import select
from src.db.main import get_session
from sqlmodel.ext.asyncio.session import AsyncSession
from typing import List
from uuid import UUID
from src.db.models import DailyChallenge, User
from src.auth.dependencies import get_current_user

daily_challenge_router = APIRouter()

# DailyChallenge Endpoints

@daily_challenge_router.get("/api/challenges", response_model=List[DailyChallenge])
def get_challenges(session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    challenges = session.exec(select(DailyChallenge).where(DailyChallenge.created_by == current_user.id)).all()
    return challenges

@daily_challenge_router.get("/api/challenges/{challenge_id}", response_model=DailyChallenge)
def get_challenge(challenge_id: UUID, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    challenge = session.get(DailyChallenge, challenge_id)
    if not challenge:
        raise HTTPException(status_code=404, detail="Challenge not found")
    if challenge.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this challenge")
    return challenge

@daily_challenge_router.post("/api/challenges", response_model=DailyChallenge)
def create_challenge(challenge: DailyChallenge, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    challenge.created_by = current_user.id
    session.add(challenge)
    session.commit()
    session.refresh(challenge)
    return challenge

@daily_challenge_router.put("/api/challenges/{challenge_id}", response_model=DailyChallenge)
def update_challenge(challenge_id: UUID, challenge_update: DailyChallenge, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    challenge = session.get(DailyChallenge, challenge_id)
    if not challenge:
        raise HTTPException(status_code=404, detail="Challenge not found")
    if challenge.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to update this challenge")
    for key, value in challenge_update.dict().items():
        setattr(challenge, key, value)
    session.commit()
    session.refresh(challenge)
    return challenge

@daily_challenge_router.delete("/api/challenges/{challenge_id}")
def delete_challenge(challenge_id: UUID, session: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
    challenge = session.get(DailyChallenge, challenge_id)
    if not challenge:
        raise HTTPException(status_code=404, detail="Challenge not found")
    if challenge.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this challenge")
    session.delete(challenge)
    session.commit()
    return {"message": "Challenge deleted successfully"}