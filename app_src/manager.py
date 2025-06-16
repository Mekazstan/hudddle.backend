from typing import Dict, List
import uuid
from fastapi.websockets import WebSocket
from collections import defaultdict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, exists
from app_src.db.models import WorkroomLiveSession, User, WorkroomMemberLink
from datetime import datetime
from app_src.services.nosql_client import NoSQLClient 
import json

class WebSocketManager:
    def __init__(self, max_connections=50, rate_limit=30):
        self.MAX_CONNECTIONS_PER_ROOM = max_connections
        self.RATE_LIMIT = rate_limit
        self.active_connections: Dict[str, Dict[str, WebSocket]] = defaultdict(dict)
        self.active_sessions: Dict[str, str] = {}  # workroom_id: session_id
        self.user_data_cache: Dict[str, Dict] = {}
        self.message_rates = defaultdict(lambda: defaultdict(int))
        self.last_reset = datetime.utcnow()
        self.presence_data = defaultdict(dict)
        self.nosql = NoSQLClient()
        
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
        
    async def connect(self, websocket: WebSocket, workroom_id: str, user_id: str, session: AsyncSession):
        """Handle new WebSocket connection"""
        # Check connection limit
        if len(self.active_connections.get(workroom_id, {})) >= MAX_CONNECTIONS_PER_ROOM:
            await websocket.send_json({
                'type': 'error',
                'message': 'Workroom is at maximum capacity'
            })
            await websocket.close()
            return
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
        await self.send_session_state(websocket, workroom_id, session)
        self.presence_data[workroom_id][user_id] = {
            'last_active': datetime.utcnow(),
            'status': 'online'
        }
        await self.broadcast_presence_update(workroom_id)
        
    async def send_session_state(self, websocket: WebSocket, workroom_id: str, live_session: WorkroomLiveSession, session: AsyncSession):
        """Send current session state to a client"""
        # Get all participants
        participants = []
        for user_id in self.active_connections[workroom_id].keys():
            user_data = await self.get_user_data(user_id, session)
            participants.append(user_data)
            # print(f"Participant Data for {user_id}: {user_data}")
            
        # Get last 50 messages from NoSQL
        messages = await self.nosql.get_recent_messages(workroom_id, limit=50)
        
        # Enrich messages with sender data
        enriched_messages = []
        for msg in messages:
            sender_data = await self.get_user_data(msg['sender_id'], session)
            enriched_messages.append({
                'id': msg['id'],
                'sender': sender_data,
                'content': msg['content'],
                'timestamp': msg['timestamp'],
                'metadata': msg.get('metadata', {})
            })
            
        session_state = {
            'type': 'session_state',
            'is_active': live_session.is_active,
            'screen_sharer': await self.get_user_data(live_session.screen_sharer_id, session) if live_session.screen_sharer_id else None,
            'participants': participants,
            'messages': enriched_messages
        }
        await websocket.send_json(session_state)
        
    async def disconnect(self, websocket: WebSocket, workroom_id: str, user_id: str, session: AsyncSession):
        """Handle WebSocket disconnection"""
        try:
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
                    
                if workroom_id in self.presence_data and user_id in self.presence_data[workroom_id]:
                    self.presence_data[workroom_id][user_id]['status'] = 'offline'
                    self.presence_data[workroom_id][user_id]['last_active'] = datetime.utcnow()
                await self.broadcast_presence_update(workroom_id)
        except Exception as e:
            print(f"Error during disconnect: {str(e)}")
        finally:
            # Ensure connection is removed even if errors occur
            self.active_connections[workroom_id].pop(user_id, None)
            self.presence_data[workroom_id].pop(user_id, None)
                
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
        # Reset counters every minute
        if (datetime.utcnow() - self.last_reset).total_seconds() > 60:
            self.message_rates.clear()
            self.last_reset = datetime.utcnow()
        
        # Rate limit (e.g., 30 messages per minute)
        if self.message_rates[user_id][workroom_id] > 30:
            await self.active_connections[workroom_id][user_id].send_json({
                'type': 'error',
                'message': 'Message rate limit exceeded'
            })
            return
        
        self.message_rates[user_id][workroom_id] += 1
        message_type = data.get('type')
        
        if message_type == 'chat':
            await self.handle_chat_message(data, workroom_id, user_id, session)
        elif message_type == 'edit_message':
            await self.handle_edit_message(data, workroom_id, user_id, session)
        elif message_type == 'delete_message':
            await self.handle_delete_message(data, workroom_id, user_id, session)
        elif message_type == 'typing':
            await self.handle_typing_indicator(data, workroom_id, user_id, session)
            
    async def handle_edit_message(self, data: dict, workroom_id: str, user_id: str, session: AsyncSession):
        """Handle message editing"""
        message_id = data['message_id']
        new_content = data['content']
        
        # Verify message exists and belongs to user
        message = await self.nosql.get_message(message_id)
        if not message or message['sender_id'] != user_id:
            await self.active_connections[workroom_id][user_id].send_json({
                'type': 'error',
                'message': 'Cannot edit this message'
            })
            return
        
        # Update in NoSQL
        success = await self.nosql.edit_message(message_id, new_content)
        if success:
            user_data = await self.get_user_data(user_id, session)
            await self.broadcast(workroom_id, {
                'type': 'message_edited',
                'message_id': message_id,
                'new_content': new_content,
                'edited_by': user_data,
                'edited_at': datetime.utcnow().isoformat()
            })

    async def handle_delete_message(self, data: dict, workroom_id: str, user_id: str, session: AsyncSession):
        """Handle message deletion"""
        message_id = data['message_id']
        
        # Verify message exists and belongs to user (or user is admin)
        message = await self.nosql.get_message(message_id)
        if not message or message['sender_id'] != user_id:
            await self.active_connections[workroom_id][user_id].send_json({
                'type': 'error',
                'message': 'Cannot delete this message'
            })
            return
        
        # Soft delete in NoSQL
        success = await self.nosql.delete_message(message_id)
        if success:
            user_data = await self.get_user_data(user_id, session)
            await self.broadcast(workroom_id, {
                'type': 'message_deleted',
                'message_id': message_id,
                'deleted_by': user_data,
                'deleted_at': datetime.utcnow().isoformat()
            })
    
    async def handle_chat_message(self, data: dict, workroom_id: str, sender_id: str, session: AsyncSession):
        """Handle chat messages with NoSQL storage"""
        content = data.get('content', '').strip()
        if not content:
            await self.active_connections[workroom_id][sender_id].send_json({
                'type': 'error', 
                'message': 'Message cannot be empty'
            })
            return
        if len(content) > 2000:
            await self.active_connections[workroom_id][sender_id].send_json({
                'type': 'error',
                'message': 'Message too long (max 2000 characters)'
            })
            return
        try:
            user_data = await self.get_user_data(sender_id, session)
            
            # Create message document
            message_id = str(uuid.uuid4())
            message_doc = {
                'id': message_id,
                'workroom_id': workroom_id,
                'sender_id': sender_id,
                'content': data['content'],
                'timestamp': datetime.utcnow().isoformat(),
                'metadata': {
                    'edited': False,
                    'deleted': False
                }
            }
            
            # Store in NoSQL
            await self.nosql.insert_message(workroom_id, message_doc)
            
            # Broadcast with sender info
            await self.broadcast(workroom_id, {
                'type': 'chat',
                'id': message_id,
                'sender': user_data,
                'content': data['content'],
                'timestamp': message_doc['timestamp']
            })
        except Exception as e:
            # Handle any errors (e.g., invalid data, NoSQL issues)
            await self.active_connections[workroom_id][sender_id].send_json({
                'type': 'error',
                'message': f'Failed to send message: {str(e)}'
            })
            return
                    
    async def handle_typing_indicator(self, data: dict, workroom_id: str, sender_id: str, session: AsyncSession):
        """Handle typing indicators"""
        user_data = await self.get_user_data(sender_id, session)
        
        await self.broadcast(workroom_id, {
            'type': 'typing',
            'user': user_data,
            'is_typing': data.get('is_typing', False)
        }, exclude=[sender_id])
        
    async def broadcast_presence_update(self, workroom_id: str):
        await self.broadcast(workroom_id, {
            'type': 'presence_update',
            'users': self.presence_data.get(workroom_id, {})
        })
        
    async def get_message_history(self, workroom_id: str, before: datetime = None, limit: int = 50):
        """Get message history with pagination"""
        return await self.nosql.get_message_history(workroom_id, before, limit)
    
    async def add_reaction(self, message_id: str, user_id: str, emoji: str):
        """Add reaction to a message"""
        await self.nosql.add_reaction(message_id, user_id, emoji)

    async def remove_reaction(self, message_id: str, user_id: str, emoji: str):
        """Remove reaction from a message"""
        await self.nosql.remove_reaction(message_id, user_id, emoji)

    async def mark_as_read(self, message_id: str, user_id: str):
        """Mark message as read by user"""
        await self.nosql.mark_as_read(message_id, user_id)
        
    async def handle_read_receipt(self, data: dict, workroom_id: str, user_id: str):
        message_id = data['message_id']
        await self.nosql.mark_as_read(message_id, user_id)
        await self.broadcast(workroom_id, {
            'type': 'read_receipt',
            'message_id': message_id,
            'read_by': user_id,
            'timestamp': datetime.utcnow().isoformat()
        })
        
    async def cleanup(self):
        """Clean up resources"""
        await self.nosql.client.close()