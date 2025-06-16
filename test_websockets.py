import pytest
from fastapi.testclient import TestClient
from websockets.client import connect
from app_src import manager
from app_src.main import app
import json
import jwt
from app_src.config import Config

@pytest.fixture
def test_client():
    return TestClient(app)

def get_user_id_from_token(token: str) -> str:
    """Extract user_id from JWT token"""
    payload = jwt.decode(token, Config.SECRET_KEY, algorithms=[Config.ALGORITHM])
    return str(payload.get("sub"))

@pytest.fixture
def auth_token(test_client):
    # Get auth token for testing (modify based on your auth system)
    response = test_client.post(
        "/api/v1/auth/login",
        json={"email": "test@example.com", "password": "testpassword"}
    )
    token = response.json()["access_token"]
    user_id = get_user_id_from_token(token)
    return {"token": token, "user_id": user_id}

@pytest.mark.asyncio
async def test_websocket_connection(test_client, auth_token):
    # First get the workroom ID you have access to
    response = test_client.get(
        "/api/v1/workrooms",
        headers={"Authorization": f"Bearer {auth_token}"}
    )
    workroom_id = response.json()[0]["id"]  # Assuming you have at least one workroom
    
    # Connect to WebSocket
    async with connect(
        f"ws://testserver/api/v1/workrooms/{workroom_id}/ws?token={auth_token}"
    ) as websocket:
        # Test initial connection
        response = await websocket.recv()
        assert json.loads(response)["type"] in ["session_state", "presence_update"]
        
@pytest.mark.asyncio
async def test_chat_messages(test_client, auth_token):
    response = test_client.get(
        "/api/v1/workrooms",
        headers={"Authorization": f"Bearer {auth_token}"}
    )
    workroom_id = response.json()[0]["id"]
    
    async with connect(
        f"ws://testserver/api/v1/workrooms/{workroom_id}/ws?token={auth_token}"
    ) as websocket:
        # Send a chat message
        await websocket.send(json.dumps({
            "type": "chat",
            "content": "Hello, world!"
        }))
        
        # Verify the message is broadcasted back
        response = await websocket.recv()
        message = json.loads(response)
        assert message["type"] == "chat"
        assert message["content"] == "Hello, world!"
        
        
@pytest.mark.asyncio
async def test_presence_updates(test_client, auth_token):
    token = auth_token["token"]
    user_id = auth_token["user_id"]
    
    response = test_client.get(
        "/api/v1/workrooms",
        headers={"Authorization": f"Bearer {auth_token}"}
    )
    workroom_id = response.json()[0]["id"]
    
    async with connect(
        f"ws://testserver/api/v1/workrooms/{workroom_id}/ws?token={auth_token}"
    ) as websocket:
        # Initial presence update
        response = await websocket.recv()
        data = json.loads(response)
        if data["type"] == "session_state":
            # Get the next message for presence update
            response = await websocket.recv()
            data = json.loads(response)
        
        assert data["type"] == "presence_update"
        assert user_id in data["users"]  # Verify current user is present
        
@pytest.mark.asyncio
async def test_invalid_token(test_client):
    with pytest.raises(Exception):  # WebSocket connection should fail
        async with connect(
            "ws://testserver/api/v1/workrooms/some-id/ws?token=invalid"
        ) as websocket:
            await websocket.recv()

@pytest.mark.asyncio
async def test_rate_limiting(test_client, auth_token):
    response = test_client.get(
        "/api/v1/workrooms",
        headers={"Authorization": f"Bearer {auth_token}"}
    )
    workroom_id = response.json()[0]["id"]
    
    async with connect(
        f"ws://testserver/api/v1/workrooms/{workroom_id}/ws?token={auth_token}"
    ) as websocket:
        # Send more than 30 messages quickly
        for i in range(31):
            await websocket.send(json.dumps({
                "type": "chat",
                "content": f"Message {i}"
            }))
            if i < 30:
                await websocket.recv()  # Consume responses
        
        # Should receive rate limit error
        response = await websocket.recv()
        assert json.loads(response)["type"] == "error"
        
        
@pytest.mark.asyncio
async def test_multiple_clients(test_client, auth_token):
    token1 = auth_token["token"]
    user_id1 = auth_token["user_id"]
    
    response = test_client.get(
        "/api/v1/workrooms",
        headers={"Authorization": f"Bearer {auth_token}"}
    )
    workroom_id = response.json()[0]["id"]
    
    # Create second test user
    response = test_client.post(
        "/api/v1/auth/register",
        json={"email": "test2@example.com", "password": "testpassword"}
    )
    token2 = response.json()["access_token"]
    user_id2 = get_user_id_from_token(token2)
    
    async with connect(
        f"ws://testserver/api/v1/workrooms/{workroom_id}/ws?token={token1}"
    ) as ws1, connect(
        f"ws://testserver/api/v1/workrooms/{workroom_id}/ws?token={token2}"
    ) as ws2:
        # Both clients should receive presence updates
        await ws1.recv()  # Initial session state
        presence1 = json.loads(await ws1.recv())
        
        # Verify both users are in presence data
        assert user_id1 in presence1["users"]
        assert user_id2 in presence1["users"]
        
        # Send message from ws1, ws2 should receive it
        await ws1.send(json.dumps({
            "type": "chat",
            "content": "Hello from user 1"
        }))
        
        # Both should receive the message
        msg1 = json.loads(await ws1.recv())
        msg2 = json.loads(await ws2.recv())
        
        assert msg1["content"] == "Hello from user 1"
        assert msg2["content"] == "Hello from user 1"
        
        
@pytest.mark.asyncio
async def test_disconnection(test_client, auth_token):
    token = auth_token["token"]
    user_id = auth_token["user_id"]
    
    response = test_client.get(
        "/api/v1/workrooms",
        headers={"Authorization": f"Bearer {auth_token}"}
    )
    workroom_id = response.json()[0]["id"]
    
    async with connect(
        f"ws://testserver/api/v1/workrooms/{workroom_id}/ws?token={auth_token}"
    ) as websocket:
        # Get initial messages
        await websocket.recv()  # session state
        await websocket.recv()  # presence update
        
        # Close connection abruptly
        await websocket.close()
        
        # Verify manager cleaned up properly (you might need to expose some internal state for testing)
        assert user_id not in manager.active_connections.get(workroom_id, {})
        
