from fastapi import FastAPI
from .auth.routes import auth_router
from .daily_challenge.routes import daily_challenge_router
from .leaderboard.routes import leaderboard_router
from .tasks.routes import task_router
from .team.routes import team_router
from .workroom.routes import workroom_router
from .middleware import register_middleware
from contextlib import asynccontextmanager
from src.db.main import init_db
from src.db.mongo import initialize_mongo

@asynccontextmanager 
async def life_span(app:FastAPI):
    print(f"Server is starting...")
    await init_db()
    await initialize_mongo()
    yield
    print(f"Server has been stopped")

version = "v1"

version_prefix =f"/api/{version}"


app = FastAPI(
    title = "Hudddle Web Service",
    description = "Let's make working fun 🤪😉",
    version= version,
    lifespan= life_span,
    openapi_url=f"{version_prefix}/openapi.json",
    docs_url=f"{version_prefix}/docs",
    redoc_url=f"{version_prefix}/redoc"
)

register_middleware(app)


app.include_router(auth_router, prefix=f"/api/{version}/auth", tags=['auth'])
app.include_router(task_router, prefix=f"/api/{version}/tasks", tags=['tasks'])
app.include_router(workroom_router, prefix=f"/api/{version}/workrooms", tags=['workrooms'])
app.include_router(daily_challenge_router, prefix=f"/api/{version}/daily_challenges", tags=['daily_challenges'])
app.include_router(leaderboard_router, prefix=f"/api/{version}/leaderboards", tags=['leaderboards'])
app.include_router(team_router, prefix=f"/api/{version}/teams", tags=['teams'])



