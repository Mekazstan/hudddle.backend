from fastapi import FastAPI
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware
from config import Config
import time
import logging

logger = logging.getLogger("uvicorn.access")
logger.disabled = False

def register_middleware(app: FastAPI):
    app.add_middleware(
        SessionMiddleware,
        secret_key=Config.JWT_SECRET_KEY,
        session_cookie="session",
    )

    # Custom logging middleware
    @app.middleware("http")
    async def custom_logging(request: Request, call_next):
        start_time = time.time()
        print("Before", start_time)

        response = await call_next(request)
        processing_time = time.time() - start_time
        message = f"{request.client.host}:{request.client.port} - {request.method} - {request.url.path} - {response.status_code} completed after {processing_time}s"
        print(message)

        return response

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    # TrustedHost middleware
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["*"],
    )
