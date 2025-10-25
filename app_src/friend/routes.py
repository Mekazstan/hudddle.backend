from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import List
from app_src.db.models import FriendLink, FriendRequest, FriendRequestStatus, User
from .schema import FriendRequestResponseSchema, FriendRequestSchema, AcceptFriendRequestSchema
from app_src.auth.schema import UserSchema
from app_src.auth.dependencies import get_current_user
from app_src.db.db_connect import get_session
from app_src.auth.service import UserService
from arq.connections import ArqRedis
from app_src.redis_config import get_redis_pool


user_service = UserService() 
friend_router = APIRouter()

# Friend Endpoints

@friend_router.post("/friends/request")
async def send_friend_request(
    request_data: FriendRequestSchema,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis: ArqRedis = Depends(get_redis_pool),
):
    receiver_email = request_data.receiver_email.lower()

    if current_user.email.lower() == receiver_email:
        raise HTTPException(status_code=400, detail="You cannot send a friend request to yourself.")
    
    receiver = await user_service.get_user_by_email(receiver_email, session)
    if not receiver:
        # User doesn't exist - send invitation email
        print(f"📧 User {receiver_email} not found. Sending invitation email.")
        
        try:
            await redis.enqueue_job(
                'send_friend_request_invite',
                current_user.first_name or current_user.email,
                current_user.email,
                receiver_email
            )
            
            return {
                "status": "invitation_sent",
                "message": f"Invitation email sent to {receiver_email}",
                "receiver_email": receiver_email,
                "is_new_user": True
            }
        except Exception as e:
            print(f"❌ Failed to queue invitation email: {e}")
            raise HTTPException(
                status_code=500,
                detail="Failed to send invitation email"
            )
    
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
    return {
        "status": "request_sent",
        "message": "Friend request sent successfully",
        "request_id": new_request.id,
        "receiver_email": receiver.email,
        "is_new_user": False
    }

@friend_router.post("/friends/request/accept")
async def accept_friend_request_by_email(
    request_data: AcceptFriendRequestSchema,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    sender_email = request_data.sender_email.lower()

    if current_user.email.lower() == sender_email:
        raise HTTPException(status_code=400, detail="You cannot accept your own friend request.")

    sender = await user_service.get_user_by_email(sender_email, session)
    if not sender:
        raise HTTPException(status_code=404, detail="Sender not found")

    # Find the pending friend request from that sender to the current user
    result = await session.execute(
        select(FriendRequest).where(
            FriendRequest.sender_id == sender.id,
            FriendRequest.receiver_id == current_user.id,
            FriendRequest.status == FriendRequestStatus.pending
        )
    )
    friend_request = result.scalars().first()

    if not friend_request:
        raise HTTPException(status_code=404, detail="No pending friend request from this user")

    # Accept the friend request
    friend_request.status = FriendRequestStatus.accepted
    friend_request.updated_at = datetime.utcnow()
    session.add(friend_request)

    # Create reciprocal friend links
    session.add_all([
        FriendLink(user_id=friend_request.sender_id, friend_id=friend_request.receiver_id),
        FriendLink(user_id=friend_request.receiver_id, friend_id=friend_request.sender_id)
    ])

    await session.commit()
    return {"message": "Friend request accepted"}

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

@friend_router.get("/friends/requests/pending", response_model=List[FriendRequestResponseSchema])
async def get_pending_friend_requests(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    result = await session.execute(
        select(FriendRequest)
        .options(selectinload(FriendRequest.sender))
        .where(
            FriendRequest.receiver_id == current_user.id,
            FriendRequest.status == FriendRequestStatus.pending
        )
    )
    pending_requests = result.scalars().all()

    # Return with sender's email attached
    return [
        FriendRequestResponseSchema(
            id=req.id,
            sender_id=req.sender_id,
            sender_email=req.sender.email,
            status=req.status,
            created_at=req.created_at
        ) for req in pending_requests
    ]
