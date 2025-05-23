from typing import Dict, List, Optional
from fastapi import status
from fastapi.websockets import WebSocket
from collections import defaultdict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, exists
from app_src.db.models import WorkroomLiveSession, User, WorkroomMemberLink
from datetime import datetime
import json

class WebSocketManager:
    def __init__(self):
        self.active_connections: Dict[str, Dict[str, WebSocket]] = defaultdict(dict)
        self.active_sessions: Dict[str, str] = {}  # workroom_id: session_id
        self.user_data_cache: Dict[str, Dict] = {}
        
    async def get_user_data(self, user_id: str, session: AsyncSession) -> Dict:
        """Get user data from cache or database"""
        if user_id in self.user_data_cache:
            return self.user_data_cache[user_id]
            
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalars().first()
        if not user:
            return {}
            
        user_data = {
            'id': str(user.id),
            'username': user.username,
            'avatar_url': user.avatar_url,
            'first_name': user.first_name,
            'last_name': user.last_name
        }
        self.user_data_cache[user_id] = user_data
        return user_data
        
    async def verify_workroom_access(self, user_id: str, workroom_id: str, session: AsyncSession) -> bool:
        """Check if user has access to the workroom"""
        result = await session.execute(
            select(exists().where(
                WorkroomMemberLink.workroom_id == workroom_id,
                WorkroomMemberLink.user_id == user_id
            ))
        )
        return result.scalars().first()
        
    async def get_or_create_live_session(self, workroom_id: str, session: AsyncSession) -> WorkroomLiveSession:
        """Get active session or create new one"""
        result = await session.execute(
            select(WorkroomLiveSession)
            .where(
                WorkroomLiveSession.workroom_id == workroom_id,
                WorkroomLiveSession.is_active == True
            )
        )
        live_session = result.scalars().first()
        
        if not live_session:
            live_session = WorkroomLiveSession(
                workroom_id=workroom_id,
                is_active=True
            )
            session.add(live_session)
            await session.commit()
            await session.refresh(live_session)
            
        return live_session
        
    async def update_screen_sharer(self, workroom_id: str, user_id: Optional[str], session: AsyncSession):
        """Update who is sharing their screen"""
        live_session = await self.get_or_create_live_session(workroom_id, session)
        live_session.screen_sharer_id = user_id
        await session.commit()
        
        # Notify all clients
        await self.broadcast(workroom_id, {
            'type': 'screen_share_update',
            'screen_sharer_id': user_id
        })
        
    async def end_live_session(self, workroom_id: str, session: AsyncSession):
        """Mark session as ended"""
        live_session = await self.get_or_create_live_session(workroom_id, session)
        live_session.is_active = False
        live_session.ended_at = datetime.utcnow()
        await session.commit()
        
        # Notify all clients
        await self.broadcast(workroom_id, {
            'type': 'session_end',
            'message': 'Live session has ended'
        })
        
    async def connect(self, websocket: WebSocket, workroom_id: str, user_id: str, session: AsyncSession):
        """Handle new WebSocket connection"""
        await websocket.accept()
        
        # Verify access
        if not await self.verify_workroom_access(user_id, workroom_id, session):
            # Send access denied message instead of closing
            await websocket.send_json({
                'type': 'access_denied',
                'message': 'You do not have access to this workroom',
                'workroom_id': workroom_id,
                'suggestion': 'Please request access from the workroom owner'
            })
            return
            
        # Get or create live session
        live_session = await self.get_or_create_live_session(workroom_id, session)
        
        # Add to active connections
        self.active_connections[workroom_id][user_id] = websocket
        
        # Get user data
        user_data = await self.get_user_data(user_id, session)
        
        # Notify others about new participant
        await self.broadcast(workroom_id, {
            'type': 'presence',
            'action': 'join',
            'user': user_data,
            'timestamp': datetime.utcnow().isoformat()
        }, exclude=[user_id])
        
        # Send current session state to new participant
        await self.send_session_state(websocket, workroom_id, live_session, session)
        
    async def send_session_state(self, websocket: WebSocket, workroom_id: str, live_session: WorkroomLiveSession, session: AsyncSession):
        """Send current session state to a client"""
        # Get all participants
        participants = []
        for user_id in self.active_connections[workroom_id].keys():
            user_data = await self.get_user_data(user_id, session)
            participants.append(user_data)
            # print(f"Participant Data for {user_id}: {user_data}")
            
        # Get screen sharer data if any
        screen_sharer_data = None
        if live_session.screen_sharer_id:
            screen_sharer_data = await self.get_user_data(live_session.screen_sharer_id, session)
            # print(f"Screen Sharer Data: {screen_sharer_data}")
            
        session_state = {
            'type': 'session_state',
            'is_active': live_session.is_active,
            'screen_sharer': screen_sharer_data,
            'participants': participants
        }
        # print(f"Sending Session State: {session_state}")
        await websocket.send_json(session_state)
        
    async def disconnect(self, websocket: WebSocket, workroom_id: str, user_id: str, session: AsyncSession):
        """Handle WebSocket disconnection"""
        if workroom_id in self.active_connections and user_id in self.active_connections[workroom_id]:
            del self.active_connections[workroom_id][user_id]
            
            # Get user data
            user_data = await self.get_user_data(user_id, session)
            
            # Notify others about participant leaving
            await self.broadcast(workroom_id, {
                'type': 'presence',
                'action': 'leave',
                'user': user_data,
                'timestamp': datetime.utcnow().isoformat()
            })
            
            # If this was the screen sharer, clear the screen sharer
            live_session = await self.get_or_create_live_session(workroom_id, session)
            if live_session.screen_sharer_id == user_id:
                await self.update_screen_sharer(workroom_id, None, session)
                
            # If no more participants, end session
            if not self.active_connections[workroom_id]:
                await self.end_live_session(workroom_id, session)
                
    async def broadcast(self, workroom_id: str, message: dict, exclude: List[str] = None):
        """Send message to all clients in workroom except those in exclude list"""
        if workroom_id not in self.active_connections:
            return
            
        exclude = exclude or []
        for user_id, connection in self.active_connections[workroom_id].items():
            if user_id not in exclude:
                try:
                    await connection.send_json(message)
                except:
                    # Handle disconnected clients
                    pass
                    
    async def handle_message(self, data: dict, workroom_id: str, user_id: str, session: AsyncSession):
        """Handle incoming WebSocket messages"""
        message_type = data.get('type')
        
        if message_type == 'chat':
            await self.handle_chat_message(data, workroom_id, user_id, session)
        elif message_type == 'screen_share':
            await self.handle_screen_share(data, workroom_id, user_id, session)
        elif message_type == 'typing':
            await self.handle_typing_indicator(data, workroom_id, user_id, session)
            
    async def handle_chat_message(self, data: dict, workroom_id: str, sender_id: str, session: AsyncSession):
        """Handle chat messages"""
        user_data = await self.get_user_data(sender_id, session)
        
        message = {
            'type': 'chat',
            'sender': user_data,
            'content': data['content'],
            'timestamp': datetime.utcnow().isoformat()
        }
        await self.broadcast(workroom_id, message)
        
    async def handle_screen_share(self, data: dict, workroom_id: str, user_id: str, session: AsyncSession):
        """Handle screen sharing events and WebRTC signaling"""
        action = data.get('action')
        
        if action == 'start':
            # Notify all participants about new screen share
            await self.broadcast(workroom_id, {
                'type': 'screen_share_update',
                'screen_sharer_id': user_id,
                'signal': data.get('signal')
            })
            
            # Update screen sharer in database
            await self.update_screen_sharer(workroom_id, user_id, session)
            
        elif action == 'stop':
            await self.broadcast(workroom_id, {
                'type': 'screen_share_update',
                'screen_sharer_id': None
            })
            await self.update_screen_sharer(workroom_id, None, session)
            
        elif action == 'signal':
            # Forward WebRTC signaling messages to specific participant
            target_user = data.get('target_user')
            if target_user and target_user in self.active_connections[workroom_id]:
                await self.send_to_user(workroom_id, target_user, {
                    'type': 'webrtc_signal',
                    'signal': data.get('signal'),
                    'sender': user_id
                })
            
    async def handle_typing_indicator(self, data: dict, workroom_id: str, sender_id: str, session: AsyncSession):
        """Handle typing indicators"""
        user_data = await self.get_user_data(sender_id, session)
        
        await self.broadcast(workroom_id, {
            'type': 'typing',
            'user': user_data,
            'is_typing': data.get('is_typing', False)
        }, exclude=[sender_id])
        
        