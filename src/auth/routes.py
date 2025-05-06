from mail import create_message, mail
from fastapi import (APIRouter, Depends, status, Response, File,
                     HTTPException, BackgroundTasks, UploadFile)
from fastapi.responses import JSONResponse
from sqlalchemy import insert, delete
from sqlalchemy.ext.asyncio import AsyncSession
import logging

# Import SQLAlchemy models and utilities
from db.models import User, PasswordResetOTP
from db.db_connect import get_session
from .schema import (
    ForgotPassword, Message, PasswordResetOTPRequest,
    UserCreateModel, UserLoginModel, UserUpdateModel, AuthToken,
    PasswordResetConfirmModel, GoogleSignIn, UserSchema
)
from .service import UserService
from .utils import (generate_password_hash, create_access_token, 
                    verify_google_token, verify_password)
from .dependencies import (AccessTokenBearer, get_current_user, 
                           RoleChecker, get_current_user_model)
from config import Config
import random
from datetime import datetime, timedelta

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

async def send_email(message):
    await mail.send_message(message)

@auth_router.post("/signup", response_model=Message, status_code=status.HTTP_201_CREATED)
async def create_user_account(
    user_data: UserCreateModel,
    session: AsyncSession = Depends(get_session)
):
    try:
        email = user_data.email
        user_exists = await user_service.user_exists(email, session)
        if user_exists:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User with email {email} already exists.",
            )
        new_user = await user_service.create_user(user_data, session)
        return {"detail": "New user account created! Welcome to Hudddle IO. "}
    except HTTPException as e:
        raise e
    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )

@auth_router.post("/login", status_code=status.HTTP_200_OK)
async def login_user(
    user_login_data: UserLoginModel,
    session: AsyncSession = Depends(get_session)
):
    try:
        email = user_login_data.email
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
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session)
):
    email = email_data.email
    user = await user_service.get_user_by_email(email, session)
    if not user:
        return JSONResponse(
            content={"message": "If this email exists, you'll receive an OTP"},
            status_code=status.HTTP_200_OK
        )
        
    await session.execute(
        delete(PasswordResetOTP)
        .where(PasswordResetOTP.email == email)
    )
    
    otp = str(random.randint(1000, 9999))
    expires_at = datetime.utcnow() + timedelta(minutes=15)
    await session.execute(
        insert(PasswordResetOTP).values(
            email=email,
            otp=otp,
            expires_at=expires_at
        )
    )
    await session.commit()
    html_message = f"""
    <h1>Password Reset OTP</h1>
    <p>Your OTP code is: <strong>{otp}</strong></p>
    <p>This code expires in 15 minutes.</p>
    """
    
    message = create_message(
        recipients=[email],
        subject="Your Password Reset OTP",
        body=html_message
    )
    
    background_tasks.add_task(send_email, message)
    
    return {"message": "OTP sent to your email"}
    
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

@auth_router.put("/update-profile", response_model=UserSchema)
async def update_user_profile(
    update_data: UserUpdateModel,
    user: User = Depends(get_current_user_model),
    _: bool = Depends(role_checker),
    session: AsyncSession = Depends(get_session),
    profile_image: UploadFile = File(None),
):
    try:
        update_dict = update_data.dict(exclude_unset=True)

        if profile_image:
            # Upload the image to S3
            image_url = await user_service.upload_image_to_s3(profile_image)
            if image_url:
                update_dict["avatar_url"] = image_url
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to upload profile image",
                )

        updated_user = await user_service.update_user(user, update_dict, session)
        return UserSchema.from_orm(updated_user)
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(f"Error updating user profile: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
        
               