from src.mail import mail, create_message
from fastapi import APIRouter, Depends, status, BackgroundTasks
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from src.db.models import User
from .schema import (PasswordResetConfirmModel, GoogleUserLogin,PasswordResetRequestModel, 
                     UserCreateModel, UserLoginModel, EmailModel, UserUpdateModel)
from .service import UserService
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from src.db.main import get_session
from firebase_admin import auth
import firebase_admin
from .utils import create_access_tokens, create_url_safe_token, decode_url_safe_token, verify_password, generate_password_hash
from datetime import timedelta, datetime
from .dependencies import RefreshTokenBearer, AccessTokenBearer, get_current_user, RoleChecker
from src.db.mongo import add_jti_to_blocklist
from src.config import Config

auth_router = APIRouter() 
user_service = UserService()
role_checker = RoleChecker(["admin", "user"])

cred = firebase_admin.credentials.Certificate("hudddle-project-firebase.json")
firebase_admin.initialize_app(cred)

REFRESH_TOKEN_EXPIRY = 2


@auth_router.post("/signup", status_code=status.HTTP_201_CREATED)
async def create_user_account(user_data: UserCreateModel,
                              bg_tasks: BackgroundTasks,
                              session: AsyncSession = Depends(get_session)):
    email = user_data.email
    user_exists = await user_service.user_exists(email, session)
    if user_exists:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"User with email {email} already exist.")
    new_user = await user_service.create_user(user_data, session)
    
    token = create_url_safe_token({"email": email})

    link = f"http://{Config.DOMAIN}/api/v1/auth/verify/{token}"

    html = f"""
    <h1>Verify your Email</h1>
    <p>Please click this <a href="{link}">link</a> to verify your email</p>
    """

    emails = [email]

    subject = "Verify Your email"
    
    message = create_message(
        recipients=emails,
        subject=subject,
        body=html
    )
    
    bg_tasks.add_task(mail.send_message, message)

    return {
        "message": "Account Created! Check email to verify your account",
        "user": new_user,
    }

@auth_router.post("/google_login")
async def google_login(user: GoogleUserLogin, session: AsyncSession = Depends(get_session)):
    try:
        # 1. Verify the Google ID token (sent by the client)
        decoded_token = auth.verify_id_token(user.id_token)
        uid = decoded_token["uid"]
        email = decoded_token.get("email") # Email might not always be present

        # 2. Check if the user exists in your database
        db_user = await user_service.get_user_by_firebase_uid(uid, session) # New method in UserService

        if not db_user:
            name = decoded_token.get("name")
            first_name = None
            last_name = None
            
            if name:
                name_parts = name.split()  # Split into first and last name
                first_name = name_parts[0]
                if len(name_parts) > 1:
                    last_name = " ".join(name_parts[1:])
                    
            # 3. Create a new user if they don't exist
            user_data = {
                "firebase_uid": uid,
                "email": email,
                "username": name,
                "first_name": first_name,
                "last_name": last_name,
                "is_verified": True,
                "password_hash": generate_password_hash("default_password"),
                "badges": [],
                "avatar_url": decoded_token.get("picture")
            }
            db_user = await user_service.create_user(user_data, session)
        
        # 4. Generate your application's access and refresh tokens
        access_token = create_access_tokens(
            user_data={
                "email": db_user.email,
                "user_uid": str(db_user.id), # Assuming you have a standard uid in your db
                "role": db_user.role # Get role from your DB
            }
        )
        refresh_token = create_access_tokens(
            user_data={
                "email": db_user.email,
                "user_uid": str(db_user.id)
            },
            refresh=True,
            expiry=timedelta(days=REFRESH_TOKEN_EXPIRY)
        )

        return JSONResponse(
            content={
                "message": "Login Successful",
                "access token": access_token,
                "refresh token": refresh_token,
                "user": {
                    "email": db_user.email,
                    "uid": str(db_user.id),
                    "username": db_user.username
                }
            }
        )

    except auth.InvalidIdTokenError as e:
        raise HTTPException(status_code=400, detail="Invalid ID token")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")

@auth_router.post("/google_verify")
async def verify_token(token: str):
    try:
        # Verify the Firebase ID token
        decoded_token = auth.verify_id_token(token)
        uid = decoded_token["uid"]
        email = decoded_token["email"]

        return {"uid": uid, "email": email}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@auth_router.post("/login", status_code=status.HTTP_200_OK)
async def login_user(user_login_data: UserLoginModel,
                              session: AsyncSession = Depends(get_session)):
    email = user_login_data.email
    password = user_login_data.password
    
    user = await user_service.get_user_by_email(email, session)
    if user is not None:
        password_valid = verify_password(password, user.password_hash)
        
        if password_valid:
            access_token = create_access_tokens(
                user_data={
                    "email": user.email,
                    "user_uid": str(user.id),
                    "role": user.role
                }
            )
            
            refresh_token = create_access_tokens(
                user_data={
                    "email": user.email,
                    "user_uid": str(user.id)
                },
                refresh=True,
                expiry=timedelta(days=REFRESH_TOKEN_EXPIRY)
            )
            return JSONResponse(
                content={
                    "message": "Login Successfull",
                    "access token": access_token,
                    "refresh token": refresh_token,
                    "user": {
                        "email": user.email,
                        "uid": str(user.id),
                        "username": user.username
                    }
                }
            )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid Email or Password"
    )
 
@auth_router.get("/verify/{token}")
async def verify_user_account(token: str, session: AsyncSession = Depends(get_session)):

    token_data = decode_url_safe_token(token)

    user_email = token_data.get("email")

    if user_email:
        user = await user_service.get_user_by_email(user_email, session)

        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        await user_service.update_user(user, {"is_verified": True}, session)

        return JSONResponse(
            content={"message": "Account verified successfully"},
            status_code=status.HTTP_200_OK,
        )

    return JSONResponse(
        content={"message": "Error occured during verification"},
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )

@auth_router.get("/refresh_token")
async def get_new_access_token(token_details:dict = Depends(RefreshTokenBearer())):
    try:
        expiry_timestamp = token_details['exp']
        if datetime.fromtimestamp(expiry_timestamp) > datetime.now():
            new_access_token = create_access_tokens(
                user_data=token_details['user']
            )
            return JSONResponse(content={
                "access_token": new_access_token
            })
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or Expired Token"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )

@auth_router.get("/logout")
async def revoke_token(token_details: dict = Depends(AccessTokenBearer())):
    jti = token_details["jti"]
    await add_jti_to_blocklist(jti)
    
    return JSONResponse(
        content={
            "message": "Logged out Successfully"
        },
        status_code=status.HTTP_200_OK
    )
    
@auth_router.get("/me", response_model=User)
async def get_current_user(user = Depends(get_current_user), 
                           _: bool = Depends(role_checker),
                           session: AsyncSession = Depends(get_session)):
    statement = select(User).where(User.id == user.id).options()
    result = await session.exec(statement)
    user = result.first()

    return user
    
@auth_router.post("/password-reset-request")
async def password_reset_request(email_data: PasswordResetRequestModel):
    email = email_data.email

    token = create_url_safe_token({"email": email})

    link = f"http://{Config.DOMAIN}/api/v1/auth/password-reset-confirm/{token}"

    html_message = f"""
    <h1>Reset Your Password</h1>
    <p>Please click this <a href="{link}">link</a> to Reset Your Password</p>
    """
    subject = "Reset Your Password"

    # send_email.delay([email], subject, html_message)
    
    message = create_message(
        recipients=[email],
        subject=subject,
        body=html_message
    )
    
    await mail.send_message(message)
    
    return JSONResponse(
        content={
            "message": "Please check your email for instructions to reset your password",
        },
        status_code=status.HTTP_200_OK,
    )

@auth_router.post("/password-reset-confirm/{token}")
async def reset_account_password(
    token: str,
    passwords: PasswordResetConfirmModel,
    session: AsyncSession = Depends(get_session),
):
    new_password = passwords.new_password
    confirm_password = passwords.confirm_new_password

    if new_password != confirm_password:
        raise HTTPException(
            detail="Passwords do not match", status_code=status.HTTP_400_BAD_REQUEST
        )

    token_data = decode_url_safe_token(token)

    user_email = token_data.get("email")

    if user_email:
        user = await user_service.get_user_by_email(user_email, session)

        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        passwd_hash = generate_password_hash(new_password)
        await user_service.update_user(user, {"password_hash": passwd_hash}, session)

        return JSONResponse(
            content={"message": "Password reset Successfully"},
            status_code=status.HTTP_200_OK,
        )

    return JSONResponse(
        content={"message": "Error occured during password reset."},
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
    
@auth_router.post("/send_mail")
async def send_mail(emails: EmailModel,
                    bg_tasks: BackgroundTasks):
    emails = emails.addresses

    html = "<h1>Welcome to the app</h1>"
    subject = "Welcome to our app"
    
    message = create_message(
        recipients=emails,
        subject=subject,
        body=html
    )
    
    bg_tasks.add_task(mail.send_message, message)

    return {"message": "Email sent successfully"}

@auth_router.put("/update-profile", response_model=User)
async def update_user_profile(
    update_data: UserUpdateModel,
    user: User = Depends(get_current_user),
    _: bool = Depends(role_checker),
    session: AsyncSession = Depends(get_session),
):
    update_dict = update_data.dict(exclude_unset=True)

    updated_user = await user_service.update_user(user, update_dict, session)
    return updated_user
