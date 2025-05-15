from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from auth.utils import get_current_user_websocket
from auth.routes import auth_router
from dashboard.routes import dashboard_router
from tasks.routes import task_router
from friend.routes import friend_router
from workroom.routes import workroom_router
from achievements.routes import achievement_router
from middleware import register_middleware
from contextlib import asynccontextmanager
from db.db_connect import init_db
from manager import WebSocketManager
from db.db_connect import get_session

manager = WebSocketManager()

@asynccontextmanager 
async def life_span(app:FastAPI):
    print(f"Server is starting...")
    await init_db()
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


@app.get("/")
async def root():
    return {
        "message": "Hudddle IO",
        "project": "API for Hudddle IO ",
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
    # Authenticate user
    user = await get_current_user_websocket(websocket, token, session)
    if not user:
        return
        
    try:
        # Connect to workroom
        await manager.connect(websocket, workroom_id, str(user.id), session)
        
        # Handle messages
        while True:
            data = await websocket.receive_json()
            await manager.handle_message(data, workroom_id, str(user.id), session)
            
    except WebSocketDisconnect:
        await manager.disconnect(websocket, workroom_id, str(user.id), session)
    except Exception as e:
        print(f"WebSocket error: {e}")
        await manager.disconnect(websocket, workroom_id, str(user.id), session)