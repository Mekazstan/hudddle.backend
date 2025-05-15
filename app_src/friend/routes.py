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
from auth.service import UserService


user_service = UserService() 
friend_router = APIRouter()

# Friend Endpoints

@friend_router.post("/friends/request")
async def send_friend_request(
    request_data: FriendRequestSchema,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    receiver_email = request_data.receiver_email.lower()

    if current_user.email.lower() == receiver_email:
        raise HTTPException(status_code=400, detail="You cannot send a friend request to yourself.")
    
    receiver = await user_service.get_user_by_email(receiver_email, session)
    if not receiver:
        raise HTTPException(status_code=404, detail="Receiver not found")
    
    existing_request = await session.execute(
        select(FriendRequest).where(
            ((FriendRequest.sender_id == current_user.id) & (FriendRequest.receiver_id == receiver.id)) |
            ((FriendRequest.sender_id == receiver.id) & (FriendRequest.receiver_id == current_user.id))
        )
    )
    friend_request = existing_request.scalars().first()

    if friend_request:
        raise HTTPException(status_code=409, detail="Friend request already pending or users are already friends.")

    # Create friend request
    new_request = FriendRequest(
        sender_id=current_user.id,
        receiver_id=receiver.id,
        status=FriendRequestStatus.pending,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(new_request)
    await session.commit()
    await session.refresh(new_request)
    return new_request


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
