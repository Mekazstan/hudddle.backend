from fastapi import (APIRouter, Depends, status, Response,
                     HTTPException, UploadFile)
from fastapi.responses import JSONResponse
from sqlalchemy import insert, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
import logging

# Import SQLAlchemy models and utilities
from app_src.arq_tasks import get_password_reset_template
from app_src.db.models import User, PasswordResetOTP
from app_src.db.db_connect import get_session
from app_src.redis_config import get_redis_pool
from .schema import (
    ForgotPassword, Message, PasswordResetOTPRequest,
    UserCreateModel, UserLoginModel, AuthToken,
    PasswordResetConfirmModel, GoogleSignIn, UserSchema, UserUpdateSchema
)
from .service import UserService, upload_image_to_s3
from .utils import (generate_password_hash, create_access_token, 
                    verify_google_token, verify_password)
from .dependencies import (AccessTokenBearer, get_current_user, 
                           RoleChecker, get_current_user_model)
from app_src.config import Config
import random
from datetime import datetime, timedelta
from arq.connections import ArqRedis

# Router and service setup
auth_router = APIRouter()
user_service = UserService()
role_checker = RoleChecker(["admin", "user"])
access_token_bearer = AccessTokenBearer()

GOOGLE_CLIENT_ID = Config.GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET = Config.GOOGLE_CLIENT_SECRET
GOOGLE_REDIRECT_URI = Config.GOOGLE_REDIRECT_URI
GOOGLE_AUTH_ENDPOINT = Config.GOOGLE_AUTH_ENDPOINT
GOOGLE_TOKEN_ENDPOINT = Config.GOOGLE_TOKEN_ENDPOINT
GOOGLE_USERINFO_ENDPOINT = Config.GOOGLE_USERINFO_ENDPOINT

@auth_router.post("/signup", response_model=Message, status_code=status.HTTP_201_CREATED)
async def create_user_account(
    user_data: UserCreateModel,
    session: AsyncSession = Depends(get_session)
):
    try:
        email = user_data.email.lower()
        user_exists = await user_service.user_exists(email, session)
        if user_exists:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User with email {email} already exists.",
            )
        
        new_user = await user_service.create_user(user_data, session)
        await user_service.create_level_for_user(new_user.id, session)
        await session.commit()
        return {"detail": "New user account created! Welcome to Hudddle IO. "}
        
    except HTTPException as e:
        await session.rollback()
        raise e
    except Exception as e:
        await session.rollback()
        logging.error(f"Signup error for {user_data.email}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"Failed to create user account: {str(e)}"
        )

@auth_router.post("/login", status_code=status.HTTP_200_OK)
async def login_user(
    user_login_data: UserLoginModel,
    session: AsyncSession = Depends(get_session)
):
    try:
        email = user_login_data.email.lower()
        password = user_login_data.password
        user = await user_service.get_user_by_email(email, session)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
        password_valid = verify_password(password, user.password_hash)
        if not password_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
        if not user.is_verified:
            await user_service.update_user(user, {"is_verified": True}, session)

        await user_service.update_last_login(user, session)

        access_token = create_access_token(
            user_data={"sub": str(user.id), "email": user.email}
        )

        return AuthToken(
            access_token=access_token,
            user={"email": user.email, "uid": str(user.id)},
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred."
        )

@auth_router.post("/signin/google", response_model=AuthToken)
async def sign_in_google(
    google_data: GoogleSignIn,
    session: AsyncSession = Depends(get_session),
):
    try:
        google_user_info = await verify_google_token(google_data.google_token)
        google_email = google_user_info["email"]
        
        existing_user = await user_service.get_user_by_email(google_email, session)
        
        if not existing_user:
            new_user_data = {
                "email": google_email,
                "username": google_user_info.get("name", ""),
                "hashed_password": generate_password_hash("google_auth_no_password"),
                "is_verified": True,
                "auth_provider": "google"
            }
            user = await user_service.create_user(new_user_data, session)
        else:
            update_data = {}
            if not existing_user.username and google_user_info.get("name"):
                update_data["username"] = google_user_info.get("name")
            
            if not existing_user.is_verified:
                update_data.update({
                    "is_verified": True,
                    "auth_provider": "google"
                })
            
            if update_data:
                user = await user_service.update_user(existing_user, update_data, session)
            else:
                user = existing_user

        access_token = create_access_token(
            user_data={
                "email": user.email,
                "user_uid": str(user.id)
            }
        )
        return AuthToken(
            access_token=access_token,
            user={
                "email": user.email,
                "full_name": user.username,
                "uid": str(user.id),
                "is_verified": user.is_verified
            },
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Google sign-in failed: {str(e)}"
        )

@auth_router.post("/logout", response_model=Message, status_code=status.HTTP_200_OK)
async def logout(response: Response):
    """Logs out the current user (client-side implementation)."""
    response.delete_cookie("access_token")
    return {"detail": "Logged out successfully"}

@auth_router.get("/me", response_model=UserSchema)
async def get_me(
    user: UserSchema = Depends(get_current_user),
):
    """Retrieves the current user's details."""
    return user

@auth_router.post("/password-reset-request")
async def password_reset_request(
    email_data: ForgotPassword,
    session: AsyncSession = Depends(get_session),
    redis: ArqRedis = Depends(get_redis_pool)
):
    try:
        user = await user_service.get_user_by_email(email_data.email, session)
        if not user:
            return JSONResponse(
                content={"message": "If this email exists, you'll receive an OTP"},
                status_code=status.HTTP_200_OK
            )
        otp = str(random.randint(1000, 9999))
        expires_at = datetime.utcnow() + timedelta(minutes=15)
            
        async with session.begin():
            # Delete existing OTPs
            await session.execute(
                delete(PasswordResetOTP)
                .where(PasswordResetOTP.email == email_data.email)
            )
            
            # Store new OTP
            await session.execute(
                insert(PasswordResetOTP).values(
                    email=email_data.email,
                    otp=otp,
                    expires_at=expires_at
                )
            )
        
        # Create ARQ Redis pool
        job = await redis.enqueue_job(
            'send_email_task',
            {
                "recipients": [email_data.email],
                "subject": "Your Password Reset OTP",
                "body": get_password_reset_template(otp)
            }
        )

        logging.info(f"Password reset OTP generated for {email_data.email}, job ID: {job.job_id}")

        return {"message": "OTP sent to your email (processing in background)"}
    except SQLAlchemyError as e:
        logging.error(f"Database error during password reset: {str(e)}")
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error occurred"
        )
    except Exception as e:
        logging.error(f"Unexpected error in password reset: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred"
        )
    
@auth_router.post("/verify-reset-otp")
async def verify_reset_otp(
    otp_data: PasswordResetOTPRequest,
    session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        delete(PasswordResetOTP)
        .where(PasswordResetOTP.email == otp_data.email)
        .where(PasswordResetOTP.otp == otp_data.otp)
        .where(PasswordResetOTP.expires_at >= datetime.utcnow())
        .returning(PasswordResetOTP.email)
    )
    deleted_record = result.scalar()
    
    if not deleted_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OTP"
        )
    
    await session.commit()
    
    reset_token = create_access_token(
        {"email": otp_data.email},
        purpose="password_reset",
        expires_minutes=15
    )
    
    return {
        "message": "OTP verified successfully",
        "reset_token": reset_token
    }

@auth_router.post("/password-reset")
async def reset_password(
    reset_data: PasswordResetConfirmModel,
    token_data: User = Depends(AccessTokenBearer(required_purpose="password_reset")),
    session: AsyncSession = Depends(get_session)
):
    try:
        email = token_data["user"]["email"]
        if reset_data.new_password != reset_data.confirm_new_password:
            raise HTTPException(status_code=400, detail="Passwords don't match")
        
        user = await user_service.get_user_by_email(email, session)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        await user_service.update_user(
            user,
            {"password_hash": generate_password_hash(reset_data.new_password)},
            session
        )
        
        return {"message": "Password updated"}

    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
        
@auth_router.patch("/update-profile-data", response_model=UserSchema)
async def update_user_data(
    update_data: UserUpdateSchema,
    user: User = Depends(get_current_user_model),
    _: bool = Depends(role_checker),
    session: AsyncSession = Depends(get_session)
):
    try:
        update_dict = update_data.dict(exclude_unset=True)
        logging.info(f"Update profile data received: {update_dict}")
        updated_user = await user_service.update_user(user, update_dict, session)
        return UserSchema.from_orm(updated_user)
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(f"Error updating user data: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )

@auth_router.post("/update-profile-image", response_model=UserSchema)
async def update_profile_image(
    profile_image: UploadFile,
    user: User = Depends(get_current_user_model),
    _: bool = Depends(role_checker),
    session: AsyncSession = Depends(get_session)
):
    try:
        image_url = await upload_image_to_s3(profile_image)
        if image_url:
            update_dict = {"avatar_url": image_url}
            updated_user = await user_service.update_user(user, update_dict, session)
            return UserSchema.from_orm(updated_user)
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload profile image",
            )
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(f"Error updating profile image: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
        
