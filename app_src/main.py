import json
from fastapi import (FastAPI, WebSocket, 
                     Query, Depends, status)
from starlette.websockets import WebSocketState, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from app_src.auth.utils import get_current_user_websocket_no_accept
from app_src.auth.routes import auth_router
from app_src.dashboard.routes import dashboard_router
from app_src.tasks.routes import task_router
from app_src.friend.routes import friend_router
from app_src.workroom.routes import workroom_router
from app_src.achievements.routes import achievement_router
from app_src.payments.routes import payment_router
from app_src.middleware import register_middleware
from contextlib import asynccontextmanager
from app_src.db.db_connect import init_db
from app_src.manager import WebSocketManager
from app_src.db.db_connect import get_session
from app_src.config import Config
from langsmith import Client

manager = WebSocketManager()

langsmith_api_key = Config.LANGCHAIN_API_KEY

@asynccontextmanager 
async def life_span(app:FastAPI):
    print(f"Server is starting...")
    await init_db()
    try:
        client = Client(api_key=langsmith_api_key)
        print("LangSmith connected successfully")
    except Exception as e:
        print(f"LangSmith connection error: {str(e)}")
    yield
    print(f"Server has been stopped")

version = "v1"

version_prefix =f"/api/{version}"


app = FastAPI(
    title = "Hudddle Web Service",
    description = "Let's make working fun 😉",
    version= version,
    lifespan= life_span,
    openapi_url=f"{version_prefix}/openapi.json",
    docs_url=f"{version_prefix}/docs",
    redoc_url=f"{version_prefix}/redoc"
)

register_middleware(app)

app.include_router(auth_router, prefix=f"/api/{version}/auth", tags=['auth'])
app.include_router(dashboard_router, prefix=f"/api/{version}/dashboard", tags=['dashboard'])
app.include_router(task_router, prefix=f"/api/{version}/tasks", tags=['tasks'])
app.include_router(workroom_router, prefix=f"/api/{version}/workrooms", tags=['workrooms'])
app.include_router(friend_router, prefix=f"/api/{version}/friends", tags=['friends'])
app.include_router(achievement_router, prefix=f"/api/{version}/achievements", tags=['achievements'])
app.include_router(payment_router, prefix=f"/api/{version}/payments", tags=['payments'])


@app.get("/")
async def root():
    return {
        "message": "Hudddle IO",
        "project": "API for Hudddle IO",
        "version": app.version,
        "description": app.description,
        "documentation": {
            "swagger": "/docs",
            "redoc": "/redoc"
        }
    }

@app.websocket("/api/v1/workrooms/{workroom_id}/ws")
async def workroom_websocket_endpoint(
    websocket: WebSocket,
    workroom_id: str,
    token: str = Query(...),
    session: AsyncSession = Depends(get_session)
):
    user = None
    connection_established = False
    
    try:
        # Accept connection first
        await websocket.accept()
        connection_established = True
        print(f"WebSocket connection accepted for workroom: {workroom_id}")
        
        # Authenticate user (without accepting again)
        try:
            user = await get_current_user_websocket_no_accept(websocket, token, session)
            print(f"Authentication result: {user is not None}")
            if user:
                print(f"Authenticated user: {user.id} - {user.email}")
        except Exception as auth_error:
            print(f"Authentication error: {auth_error}")
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="Authentication failed"
                )
            return
        
        if not user:
            print("User authentication failed - no user returned")
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="Authentication failed"
                )
            return
        
        # Verify workroom access
        user_workroom_ids = [wr.id for wr in user.workrooms]
        print(f"User workrooms: {user_workroom_ids}")
        print(f"Requested workroom: {workroom_id}")
        
        if not any(str(wr.id) == workroom_id for wr in user.workrooms):
            print("User not authorized for this workroom")
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="Not authorized for this workroom"
                )
            return
        
        print("User authorized - connecting to workroom")
        
        # Check if still connected before proceeding
        if websocket.client_state != WebSocketState.CONNECTED:
            print("WebSocket disconnected before workroom connection")
            return
        
        # Connect to workroom
        try:
            await manager.connect(websocket, workroom_id, str(user.id), session)
            print("Connected to workroom successfully")
        except WebSocketDisconnect:
            print("Client disconnected during workroom connection setup")
            return
        except Exception as e:
            print(f"Error connecting to workroom: {e}")
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json({
                    'type': 'error',
                    'message': 'Failed to connect to workroom'
                })
            return
        
        # Send initial connection confirmation (if still connected)
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_json({
                    'type': 'connection_established',
                    'message': 'Successfully connected to workroom',
                    'workroom_id': workroom_id
                })
            except WebSocketDisconnect:
                print("Client disconnected while sending connection confirmation")
                return
            
        # Main message loop
        while websocket.client_state == WebSocketState.CONNECTED:
            try:
                data = await websocket.receive_json()
                print(f"Received message: {data}")
                await manager.handle_message(data, workroom_id, str(user.id), session)
            except WebSocketDisconnect:
                print("WebSocket disconnected by client")
                break
            except json.JSONDecodeError:
                print("Invalid JSON received")
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json({'error': 'Invalid JSON format'})
            except KeyError as e:
                print(f"Missing required field: {e}")
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json({'error': f'Missing required field: {str(e)}'})
            except Exception as e:
                print(f"Unexpected error in message loop: {e}")
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json({
                        'type': 'error',
                        'message': 'An unexpected error occurred'
                    })
                break
                
    except WebSocketDisconnect:
        print("WebSocket disconnected during setup")
    except Exception as e:
        print(f"Connection error: {e}")
        print(f"Error type: {type(e)}")
        import traceback
        traceback.print_exc()
        
        # Try to close gracefully if still connected
        if connection_established and websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close(
                    code=status.WS_1011_INTERNAL_ERROR,
                    reason="Internal server error"
                )
            except:
                pass
    finally:
        # Always try to disconnect from manager
        if user:
            try:
                await manager.disconnect(websocket, workroom_id, str(user.id), session)
                print("Disconnected from workroom")
            except Exception as e:
                print(f"Error during manager disconnect: {e}")

