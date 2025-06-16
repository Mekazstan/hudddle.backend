from typing import List, Dict, Optional
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient
from app_src.config import Config

class NoSQLClient:
    def __init__(self):
        self.client = AsyncIOMotorClient(Config.MONGODB_URL)
        self.db = self.client[Config.MONGODB_NAME]
        self.messages = self.db.messages
        
    async def ensure_indexes(self):
        await self.messages.create_index([('workroom_id', 1), ('timestamp', -1)])
        await self.messages.create_index([('id', 1)], unique=True)
        await self.messages.create_index([('sender_id', 1)])
        
    async def insert_message(self, workroom_id: str, message: Dict) -> str:
        """Insert a new chat message"""
        result = await self.messages.insert_one(message)
        return str(result.inserted_id)
        
    async def get_recent_messages(self, workroom_id: str, limit: int = 50) -> List[Dict]:
        """Get recent messages for a workroom"""
        cursor = self.messages.find(
            {'workroom_id': workroom_id},
            sort=[('timestamp', -1)],
            limit=limit
        )
        return await cursor.to_list(length=limit)
        
    async def edit_message(self, message_id: str, new_content: str) -> bool:
        """Edit an existing message"""
        result = await self.messages.update_one(
            {'id': message_id},
            {'$set': {
                'content': new_content,
                'metadata.edited': True,
                'metadata.edited_at': datetime.utcnow().isoformat()
            }}
        )
        return result.modified_count > 0
        
    async def delete_message(self, message_id: str) -> bool:
        """Soft delete a message"""
        result = await self.messages.update_one(
            {'id': message_id},
            {'$set': {
                'metadata.deleted': True,
                'metadata.deleted_at': datetime.utcnow().isoformat()
            }}
        )
        return result.modified_count > 0
        
    async def search_messages(self, workroom_id: str, query: str, limit: int = 20) -> List[Dict]:
        """Search messages by content"""
        cursor = self.messages.find(
            {
                'workroom_id': workroom_id,
                'content': {'$regex': query, '$options': 'i'},
                'metadata.deleted': {'$ne': True}
            },
            limit=limit
        )
        return await cursor.to_list(length=limit)
    
    async def get_message(self, message_id: str) -> Optional[Dict]:
        """Get single message by ID"""
        return await self.messages.find_one({'id': message_id})

    async def get_message_history(self, workroom_id: str, before: datetime = None, limit: int = 50) -> List[Dict]:
        """Improved pagination"""
        query = {'workroom_id': workroom_id}
        if before:
            query['timestamp'] = {'$lt': before.isoformat()}
        
        cursor = self.messages.find(
            query,
            sort=[('timestamp', -1)],
            limit=limit
        )
        return await cursor.to_list(length=limit)

    async def add_reaction(self, message_id: str, user_id: str, emoji: str):
        """Add reaction to message"""
        await self.messages.update_one(
            {'id': message_id},
            {'$addToSet': {f'reactions.{emoji}': user_id}}
        )

    async def remove_reaction(self, message_id: str, user_id: str, emoji: str):
        """Remove reaction from message"""
        await self.messages.update_one(
            {'id': message_id},
            {'$pull': {f'reactions.{emoji}': user_id}}
        )
