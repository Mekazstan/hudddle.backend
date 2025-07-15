from sqlalchemy import select
from fastapi import WebSocket, WebSocketDisconnect, status, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload
from typing import Optional
from datetime import datetime, timedelta
from passlib.context import CryptContext
from app_src.config import Config
from app_src.db.models import User
import logging
import jwt
import uuid
from jwt.exceptions import ExpiredSignatureError, DecodeError
from google.auth.transport import requests
from google.oauth2 import id_token

request = requests.Request()


password_context = CryptContext(
    schemes=["bcrypt"]
)

ACCESS_TOKEN_EXPIRY = 360000

def generate_password_hash(password: str) -> str:
    hash = password_context.hash(password)
    
    return hash

def verify_password(password: str, hash: str) -> bool:
    return password_context.verify(password, hash)

def create_access_token(user_data: dict, **kwargs):
    payload = {
        "user": user_data,
        "jti": str(uuid.uuid4()),
        "refresh": kwargs.get("refresh", False)
    }
    
    if "purpose" in kwargs:
        payload["purpose"] = kwargs["purpose"]
    
    if "expires_minutes" in kwargs:
        payload["exp"] = datetime.utcnow() + timedelta(minutes=kwargs["expires_minutes"])
    elif "expiry" in kwargs:
        payload["exp"] = datetime.utcnow() + kwargs["expiry"]
    else:
        payload["exp"] = datetime.utcnow() + timedelta(seconds=ACCESS_TOKEN_EXPIRY)
    
    return jwt.encode(payload, Config.JWT_SECRET_KEY, algorithm=Config.JWT_ALGORITHM)

def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            Config.JWT_SECRET_KEY,
            algorithms=[Config.JWT_ALGORITHM],
            options={
                "require_exp": True,
                "verify_exp": True
            }
        )
    except ExpiredSignatureError:
        logging.warning("Token has expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except DecodeError:
        logging.warning("Invalid token format")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )
    except Exception as e:
        logging.error(f"Token verification failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials"
        )

async def verify_google_token(google_token: str) -> dict:
    try:
        id_info = id_token.verify_oauth2_token(
            google_token,
            requests.Request(),
            Config.GOOGLE_CLIENT_ID
        )
        if id_info['aud'] != Config.GOOGLE_CLIENT_ID:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Google token audience"
            )
        if not id_info.get('email_verified', False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Google email not verified"
            )
        return {
            "email": id_info['email'],
            "name": id_info.get('name', ''),
            "given_name": id_info.get('given_name', ''),
            "family_name": id_info.get('family_name', ''),
            "picture": id_info.get('picture', ''),
            "sub": id_info['sub']
        }
        
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google token"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Google authentication failed: {str(e)}"
        )

async def debug_jwt_payload(token: str):
    """Debug function to inspect JWT payload structure"""
    try:
        # Decode without verification for debugging
        payload = jwt.decode(token, options={"verify_signature": False})
        print("JWT Payload structure:", payload)
        return payload
    except Exception as e:
        print(f"Debug JWT decode error: {e}")
        return None

async def get_current_user_websocket_no_accept(
    websocket: WebSocket,
    token: str,
    session: AsyncSession
) -> Optional[User]:
    """Authenticate user via WebSocket connection without accepting (already accepted)"""
    try:
        if not token:
            print("No token provided")
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="No token provided")
            return None

        try:
            # Decode the token with more detailed validation
            payload = jwt.decode(
                jwt=token,
                key=Config.JWT_SECRET_KEY,
                algorithms=[Config.JWT_ALGORITHM],
                options={
                    "require": ["exp", "user"],
                    "verify_exp": True,
                    "verify_iat": False
                }
            )
            
            # Extract user data with better error messaging
            if "user" not in payload:
                print("Token missing user data")
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token structure")
                return None
                
            # Check both possible user ID fields
            user_id = payload["user"].get("sub") or payload["user"].get("user_uid")
            if not user_id:
                print("Token missing user_uid/sub")
                print(f"Available user fields: {list(payload['user'].keys())}")
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid user data")
                return None

            # Verify user exists with explicit error handling
            try:
                result = await session.execute(
                    select(User)
                    .where(User.id == user_id)
                    .options(selectinload(User.workrooms))
                )
                user = result.scalars().first()
                
                if not user:
                    print(f"User not found: {user_id}")
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="User not found")
                    return None
                    
                print(f"User found: {user.id} - {user.email}")
                return user
                
            except SQLAlchemyError as e:
                print(f"Database error fetching user: {e}")
                await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Database error")
                return None

        except ExpiredSignatureError:
            print("Token expired")
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Token expired - please refresh"
            )
            return None
            
        except DecodeError as e:
            print(f"Token decode failed: {e}")
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Invalid token format"
            )
            return None
            
        except jwt.PyJWTError as e:
            print(f"JWT validation error: {e}")
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Token validation failed"
            )
            return None

    except WebSocketDisconnect:
        print("Client disconnected during auth")
        return None
        
    except Exception as e:
        print(f"Unexpected auth error: {e}")
        try:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Authentication error")
        except:
            pass
        return None
    
