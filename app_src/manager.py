from typing import Dict, List
import uuid
from asyncio import Lock
from fastapi.websockets import WebSocket
from collections import defaultdict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, exists
from app_src.db.models import User, WorkroomMemberLink
from datetime import datetime
from app_src.services.nosql_client import NoSQLClient 
from starlette.websockets import WebSocketState

class WebSocketManager:
    def __init__(self, max_connections=50, rate_limit=30):
        self._connections_lock = Lock()
        self._presence_lock = Lock()
        self._rate_lock = Lock()
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
        try:
            async with self._connections_lock:
                if websocket.client_state == WebSocketState.DISCONNECTED:
                    return
                self.active_connections[workroom_id][user_id] = websocket
                
            # Check connection limit
            if len(self.active_connections.get(workroom_id, {})) >= self.MAX_CONNECTIONS_PER_ROOM:
                if websocket.client_state == WebSocketState.CONNECTED:
                    try:
                        await self.safe_send(websocket, {...})
                    except RuntimeError as e:
                        if "close message" not in str(e):
                            raise
                    await websocket.close()
                return
            
            # Verify access (only if still connected)
            if websocket.client_state == WebSocketState.CONNECTED:
                if not await self.verify_workroom_access(user_id, workroom_id, session):
                    await self.safe_send(websocket, {
                        'type': 'access_denied',
                        'message': 'You do not have access to this workroom',
                        'workroom_id': workroom_id,
                        'suggestion': 'Please request access from the workroom owner'
                    })
                    return
            else:
                print(f"WebSocket disconnected during access verification for user {user_id}")
                return
            
            # Initialize data structures
            if workroom_id not in self.active_connections:
                self.active_connections[workroom_id] = {}
            if workroom_id not in self.presence_data:
                self.presence_data[workroom_id] = {}
            
            # Get user data
            user_data = await self.get_user_data(user_id, session)
            
            # Send current session state to new participant (with connection check)
            await self.send_session_state_safe(websocket, workroom_id, session)
            
            # Update presence data
            async with self._presence_lock:
                self.presence_data[workroom_id][user_id] = {
                    'last_active': datetime.utcnow(),
                    'status': 'online'
                }
            
            # Notify others about new participant (only if connection is still active)
            if websocket.client_state == WebSocketState.CONNECTED:
                await self.broadcast(workroom_id, {
                    'type': 'presence',
                    'action': 'join',
                    'user': user_data,
                    'timestamp': datetime.utcnow().isoformat()
                }, exclude=[user_id])
                
                await self.broadcast_presence_update(workroom_id)
            
            print(f"Successfully connected user {user_id} to workroom {workroom_id}")
            
        except Exception as e:
            print(f"Error in connect method: {e}")
            # Clean up on error
            if workroom_id in self.active_connections:
                self.active_connections[workroom_id].pop(user_id, None)
            if workroom_id in self.presence_data:
                self.presence_data[workroom_id].pop(user_id, None)
            raise
        
    async def send_session_state_safe(self, websocket: WebSocket, workroom_id: str, session: AsyncSession):
        """Send current session state to a client with connection checks"""
        try:
            # Check if websocket is still connected
            if websocket.client_state != WebSocketState.CONNECTED:
                print(f"WebSocket not connected, skipping session state for workroom {workroom_id}")
                return
            
            # Get all participants
            participants = []
            if workroom_id in self.active_connections:
                for user_id in self.active_connections[workroom_id].keys():
                    user_data = await self.get_user_data(user_id, session)
                    participants.append(user_data)
            
            # Get last 50 messages from NoSQL
            messages = await self.nosql.get_recent_messages(workroom_id, limit=50)
            
            # Enrich messages with sender data
            enriched_messages = []
            for msg in messages:
                try:
                    sender_data = await self.get_user_data(msg['sender_id'], session)
                    enriched_messages.append({
                        'id': msg['id'],
                        'sender': sender_data,
                        'content': msg['content'],
                        'timestamp': msg['timestamp'],
                        'metadata': msg.get('metadata', {})
                    })
                except Exception as e:
                    print(f"Error enriching message {msg.get('id')}: {e}")
                    # Skip this message or add with minimal data
                    continue
            
            session_state = {
                'type': 'session_state',
                'participants': participants,
                'messages': enriched_messages
            }
            
            # Final check before sending with more robust handling
            if websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await websocket.send_json(self._sanitize_message(session_state))
                    print(f"Session state sent successfully for workroom {workroom_id}")
                except RuntimeError as e:
                    if "close message" in str(e):
                        print(f"WebSocket was closing during send attempt: {e}")
                    else:
                        raise
            else:
                print(f"WebSocket disconnected before sending session state for workroom {workroom_id}")
                
        except Exception as e:
            print(f"Error sending session state: {e}")
        
    async def broadcast(self, workroom_id: str, message: dict, exclude: List[str] = None):
        """Send message to all clients in workroom except those in exclude list"""
        async with self._connections_lock:
            if workroom_id not in self.active_connections:
                return
            
            for user_id, ws in list(self.active_connections[workroom_id].items()):
                if user_id in exclude:
                    continue
                try:
                    if ws.client_state == WebSocketState.CONNECTED:
                        await self.safe_send(ws, message)
                except Exception as e:
                    print(f"Error broadcasting to {user_id}: {e}")
                    del self.active_connections[workroom_id][user_id]
                    
    def _sanitize_message(self, message):
        """Convert datetime objects to ISO format strings"""
        if isinstance(message, dict):
            return {k: self._sanitize_message(v) for k, v in message.items()}
        elif isinstance(message, list):
            return [self._sanitize_message(item) for item in message]
        elif isinstance(message, datetime):
            return message.isoformat()
        return message

    async def safe_send(self, websocket: WebSocket, message: dict):
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json(message)
        except RuntimeError as e:
            if "close message" not in str(e):
                raise
                
    async def disconnect(self, websocket: WebSocket, workroom_id: str, user_id: str, session: AsyncSession):
        """Handle WebSocket disconnection"""
        try:
            print(f"Disconnecting user {user_id} from workroom {workroom_id}")
            
            async with self._connections_lock:
                if workroom_id in self.active_connections and user_id in self.active_connections[workroom_id]:
                    del self.active_connections[workroom_id][user_id]
                
                # Get user data for notification
                try:
                    user_data = await self.get_user_data(user_id, session)
                    
                    # Notify others about participant leaving
                    await self.broadcast(workroom_id, {
                        'type': 'presence',
                        'action': 'leave',
                        'user': user_data,
                        'timestamp': datetime.utcnow().isoformat()
                    })
                except Exception as e:
                    print(f"Error getting user data during disconnect: {e}")
            
            # Update presence data
            async with self._presence_lock:
                if workroom_id in self.presence_data and user_id in self.presence_data[workroom_id]:
                    self.presence_data[workroom_id][user_id]['status'] = 'offline'
                    self.presence_data[workroom_id][user_id]['last_active'] = datetime.utcnow()
                
            await self.broadcast_presence_update(workroom_id)
            
        except Exception as e:
            print(f"Error during disconnect: {str(e)}")
        finally:
            # Ensure cleanup happens even if errors occur
            if workroom_id in self.active_connections:
                self.active_connections[workroom_id].pop(user_id, None)
            if workroom_id in self.presence_data:
                self.presence_data[workroom_id].pop(user_id, None)
            
            print(f"User {user_id} disconnected from workroom {workroom_id}")

    def is_websocket_active(self, websocket: WebSocket) -> bool:
        """Check if WebSocket is still active and can receive messages"""
        return websocket.client_state == WebSocketState.CONNECTED
                    
    async def handle_message(self, data: dict, workroom_id: str, user_id: str, session: AsyncSession):
        """Handle incoming WebSocket messages"""
        async with self._rate_lock:
            if (datetime.utcnow() - self.last_reset).total_seconds() > 60:
                self.message_rates.clear()
                self.last_reset = datetime.utcnow()
            
            if self.message_rates[user_id][workroom_id] > 30:
                await self.safe_send(self.active_connections[workroom_id][user_id], {...})
                return
            
            self.message_rates[user_id][workroom_id] += 1
            message_type = data.get('type')
            
            if message_type == 'chat_message':
                await self.handle_chat_message(data, workroom_id, user_id, session)
            elif message_type == 'read_receipt':
                await self.handle_read_receipt(data, workroom_id, user_id)
            elif message_type == 'load_history':
                await self.handle_load_history(data, workroom_id, user_id)
            elif message_type == 'edit_message':
                await self.handle_edit_message(data, workroom_id, user_id, session)
            elif message_type == 'delete_message':
                await self.handle_delete_message(data, workroom_id, user_id, session)
            elif message_type == 'user_typing':
                await self.handle_typing_indicator(data, workroom_id, user_id, session)
            
    async def handle_edit_message(self, data: dict, workroom_id: str, user_id: str, session: AsyncSession):
        """Handle message editing"""
        message_id = data['message_id']
        new_content = data['content']
        
        # Verify message exists and belongs to user
        message = await self.nosql.get_message(message_id)
        if not message or message['sender_id'] != user_id:
            await self.safe_send(self.active_connections[workroom_id][user_id], {
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
            await self.safe_send(self.active_connections[workroom_id][user_id], {
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
            
    def _validate_attachment(self, attachment: dict) -> bool:
        required_fields = {'name', 'type', 'url'}
        return all(field in attachment for field in required_fields)
    
    async def handle_chat_message(self, data: dict, workroom_id: str, sender_id: str, session: AsyncSession):
        """Handle chat messages with NoSQL storage"""
        print(f"Chat Message: {data}")
        content = data.get('content', '').strip()
        if not content:
            async with self._connections_lock:
                ws = self.active_connections[workroom_id].get(sender_id)

            if ws:
                await self.safe_send(ws,{
                    'type': 'error', 
                    'message': 'Message cannot be empty'
                })
            return
        if len(content) > 2000:
            async with self._connections_lock:
                ws = self.active_connections[workroom_id].get(sender_id)

            if ws:
                await self.safe_send(ws,{
                    'type': 'error',
                    'message': 'Message too long (max 2000 characters)'
                })
                return
        
        mentions = data.get('mentions', [])
        if mentions and len(mentions) > 10:
            await self.safe_send(..., {'type': 'error', 'message': 'Too many mentions'})
            return

        # Add attachment validation
        attachments = data.get('attachments', [])
        if attachments:
            for attachment in attachments:
                if not self._validate_attachment(attachment):
                    async with self._connections_lock:
                        ws = self.active_connections[workroom_id].get(sender_id)

                    if ws:
                        await self.safe_send(ws,{
                            'type': 'error',
                            'message': 'Invalid attachment'
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
                    'deleted': False,
                    'attachments': data.get('attachments', []),
                    'mentions': data.get('mentions', [])
                }
            }
            
            # Store in NoSQL
            await self.nosql.insert_message(workroom_id, message_doc)
            
            # Broadcast with sender info
            await self.broadcast(workroom_id, {
                'type': 'chat_message',
                'id': message_id,
                'sender': user_data,
                'content': data['content'],
                'timestamp': message_doc['timestamp'],
                'attachments': data.get('attachments', []),
                'mentions': data.get('mentions', [])
            })
        except Exception as e:
            # Handle any errors (e.g., invalid data, NoSQL issues)
            async with self._connections_lock:
                ws = self.active_connections[workroom_id].get(sender_id)

            if ws:
                await self.safe_send(ws,{
                    'type': 'error',
                    'message': f'Failed to send message: {str(e)}'
                })
                return
                    
    async def handle_typing_indicator(self, data: dict, workroom_id: str, sender_id: str, session: AsyncSession):
        """Handle typing indicators"""
        user_data = await self.get_user_data(sender_id, session)
        
        await self.broadcast(workroom_id, {
            'type': 'user_typing',
            'user_id': sender_id,
            'user': user_data,
            'is_typing': data.get('is_typing', False),
            'timestamp': datetime.utcnow().isoformat()
        }, exclude=[sender_id])
        
    async def broadcast_presence_update(self, workroom_id: str):
        async with self._presence_lock:
            users = self.presence_data.get(workroom_id, {}).copy()  # Avoid mutation during send
        await self.broadcast(workroom_id, {
            'type': 'presence_update',
            'users': users
        })
        
    async def handle_load_history(self, data: dict, workroom_id: str, user_id: str):
        before = datetime.fromisoformat(data['before']) if data.get('before') else None
        limit = data.get('limit', 50)
        
        messages = await self.get_message_history(workroom_id, before, limit)
        
        # Enrich with sender data
        enriched_messages = []
        for msg in messages:
            sender_data = await self.get_user_data(msg['sender_id'])
            enriched_messages.append({
                **msg,
                'sender': sender_data
            })
        
        await self.safe_send(self.active_connections[workroom_id][user_id],{
            'type': 'message_history',
            'messages': enriched_messages,
            'has_more': len(messages) == limit
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
        user_data = await self.get_user_data(user_id)
        await self.broadcast(workroom_id, {
            'type': 'message_viewed',
            'message_id': message_id,
            'viewed_by': user_data,
            'timestamp': datetime.utcnow().isoformat()
        })
        
    async def cleanup(self):
        """Clean up resources"""
        await self.nosql.client.close()