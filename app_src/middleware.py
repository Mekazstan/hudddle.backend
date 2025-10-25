from fastapi import FastAPI
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware
from app_src.config import Config
import time
import logging

logger = logging.getLogger("uvicorn.access")
logger.disabled = False

def register_middleware(app: FastAPI):
    """
    Register middleware in the correct order.
    IMPORTANT: Middleware is executed in reverse order of registration!
    First registered = Last executed (outermost layer)
    """
    
    # 1. TrustedHost middleware (outermost - first to execute)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["*"],
    )
    
    # 2. CORS middleware (must be early to handle preflight requests)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://app.hudddle.xyz",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3001",
            "http://localhost:5173",
            "http://localhost:5174",
            "http://localhost:8080",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
        expose_headers=["*"],
    )
    
    # 3. Session middleware
    app.add_middleware(
        SessionMiddleware,
        secret_key=Config.JWT_SECRET_KEY,
        session_cookie="session",
        same_site="lax",
        https_only=False,
    )

    # 4. Custom logging middleware (innermost - last to execute)
    @app.middleware("http")
    async def custom_logging(request: Request, call_next):
        start_time = time.time()
        
        # Log incoming request
        logger.info(f"Incoming request: {request.method} {request.url.path}")
        
        # Process request
        response = await call_next(request)
        
        # Calculate processing time
        processing_time = time.time() - start_time
        
        # Log response
        message = (
            f"{request.client.host}:{request.client.port} - "
            f"{request.method} - {request.url.path} - "
            f"{response.status_code} completed after {processing_time:.4f}s"
        )
        logger.info(message)

        return response
