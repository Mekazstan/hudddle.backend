from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import List
from uuid import UUID
from db.models import FriendLink, FriendRequest, FriendRequestStatus, User
from .schema import FriendRequestSchema
from auth.schema import UserSchema
from auth.dependencies import get_current_user
from db.db_connect import get_session

friend_router = APIRouter()

# Friend Endpoints

@friend_router.post("/friends/request", response_model=FriendRequestSchema)
async def send_friend_request(
    receiver_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    # Prevent sending friend request to oneself
    if current_user.id == receiver_id:
        raise HTTPException(status_code=400, detail="You cannot send a friend request to yourself.")
    
    # Check that the receiver exists
    receiver = await session.get(User, receiver_id)
    if not receiver:
        raise HTTPException(status_code=404, detail="Receiver not found")
    
    # Check if a friend request already exists between the users
    existing_request = await session.query(FriendRequest).filter(
        ((FriendRequest.sender_id == current_user.id) & (FriendRequest.receiver_id == receiver_id)) |
        ((FriendRequest.sender_id == receiver_id) & (FriendRequest.receiver_id == current_user.id))
    ).first()
    if existing_request:
        raise HTTPException(status_code=409, detail="Friend request already pending or users are already friends.")

    friend_request = FriendRequest(
        sender_id=current_user.id,
        receiver_id=receiver_id,
        status=FriendRequestStatus.pending,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(friend_request)
    await session.commit()
    await session.refresh(friend_request)
    return friend_request

@friend_router.post("/friends/request/{request_id}/accept")
async def accept_friend_request(
    request_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    friend_request = await session.get(FriendRequest, request_id)
    if not friend_request:
        raise HTTPException(status_code=404, detail="Friend request not found")
    if friend_request.receiver_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to accept this request")
    
    # Update the request status
    friend_request.status = FriendRequestStatus.accepted
    friend_request.updated_at = datetime.utcnow()
    session.add(friend_request)
    
    # Insert two rows in FriendLink for symmetry.
    friend_link1 = FriendLink(user_id=friend_request.sender_id, friend_id=friend_request.receiver_id)
    friend_link2 = FriendLink(user_id=friend_request.receiver_id, friend_id=friend_request.sender_id)
    session.add(friend_link1)
    session.add(friend_link2)
    
    await session.commit()
    return {"message": "Friend request accepted."}

@friend_router.get("/friends", response_model=List[UserSchema])
async def get_current_user_friends(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    # Fetch the current user with their friends loaded
    result = await session.execute(
        select(User)
        .options(selectinload(User.friends))
        .where(User.id == current_user.id)
    )
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user.friends

@friend_router.get("/friends/search", response_model=UserSchema)
async def get_friend_by_email(
    email: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Retrieves user data based on the provided email address.
    """
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail=f"User with email '{email}' not found")
    return user
