from motor.motor_asyncio import AsyncIOMotorClient
from src.config import Config
from datetime import datetime, timedelta

JTI_EXPIRY = 3600

async def get_mongo_client():
    mongo_client = await initialize_mongo()
    return mongo_client

# Initialize MongoDB connection
async def initialize_mongo():
    global mongo_client, blocklist_collection
    try:
        # Connect to MongoDB
        mongo_client = AsyncIOMotorClient(Config.MONGO_URI)
        db = mongo_client[Config.MONGO_DB_NAME]
        blocklist_collection = db["token_blocklist"]
        await mongo_client.admin.command('ping')
        print("Successfully connected to MongoDB")
        await create_ttl_index()  # Create TTL index
    except Exception as e:
        print(f"Error connecting to MongoDB: {e}")
        raise

        
        
# Add a JTI to the blocklist
async def add_jti_to_blocklist(jti: str) -> None:
    try:
        expiry_time = datetime.utcnow() + timedelta(seconds=JTI_EXPIRY)
        await blocklist_collection.insert_one({
            "jti": jti,
            "expiry": expiry_time
        })
        print(f"Added {jti} to blocklist with expiry at {expiry_time}")
    except Exception as e:
        print(f"Error adding to blocklist: {e}")

# Check if a JTI is in the blocklist
async def token_in_blocklist(jti: str) -> bool:
    try:
        # Find the token and ensure it hasn't expired
        token = await blocklist_collection.find_one({
            "jti": jti,
            "expiry": {"$gt": datetime.utcnow()}  # Check if expiry time is in the future
        })
        return token is not None
    except Exception as e:
        print(f"Error checking blocklist: {e}")
        return False

async def create_ttl_index():
    try:
        await blocklist_collection.create_index("expiry", expireAfterSeconds=0)
        print("TTL index created on 'expiry' field")
    except Exception as e:
        print(f"Error creating TTL index: {e}")

# Cleanup expired tokens (optional, can be run periodically)
async def cleanup_expired_tokens():
    try:
        result = await blocklist_collection.delete_many({
            "expiry": {"$lte": datetime.utcnow()}  # Delete tokens with expiry in the past
        })
        print(f"Cleaned up {result.deleted_count} expired tokens")
    except Exception as e:
        print(f"Error cleaning up expired tokens: {e}")