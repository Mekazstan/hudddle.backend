from typing import Optional
import boto3
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from fastapi import HTTPException, status, UploadFile
from db.models import User
from uuid import UUID, uuid4
from .schema import UserCreateModel
from .utils import generate_password_hash
import logging
from config import Config
from datetime import datetime
from sqlalchemy.exc import IntegrityError

class UserService:
    """
    Service class for user-related operations.
    """
    async def get_user_by_email(self, email: str, session: AsyncSession):
        """Retrieves a user by their email address."""
        try:
            stmt = select(User).where(User.email == email)
            result = await session.execute(stmt)
            user = result.scalars().first()
            return user
        except Exception as e:
            logging.error(f"Error getting user by email: {e}")
            return None

    async def get_user_by_id(self, user_id: UUID, session: AsyncSession):
        """Retrieves a user by their ID."""
        try:
            stmt = select(User).where(User.id == user_id)
            result = await session.execute(stmt)
            user = result.scalars().first()
            return user
        except Exception as e:
            logging.error(f"Error getting user by ID: {e}")
            return None

    async def user_exists(self, email: str, session: AsyncSession):
        """Checks if a user with the given email exists."""
        return await self.get_user_by_email(email, session) is not None

    async def create_user(self, user_data: UserCreateModel, session: AsyncSession):
        user_data_dict = user_data.model_dump()
        password = user_data_dict.pop("password")
        new_user = User(**user_data_dict)
        new_user.password_hash = generate_password_hash(password)
        new_user.role = "user"
        session.add(new_user)
        try:
            await session.commit()
            await session.refresh(new_user)
        except IntegrityError as e:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User with email {user_data.email} already exists."
            )
        return new_user

    async def update_user(self, user: User, user_data: dict, session: AsyncSession):
        try:
            for key, value in user_data.items():
                setattr(user, key, value)
            await session.commit()
            await session.refresh(user)
            return user
        except Exception as e:
            await session.rollback()
            logging.error(f"Error updating user: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An error occurred while updating the user."
            )
    
    async def update_last_login(self, user: User, session: AsyncSession):
        """Updates the last login timestamp for a user."""
        try:
            stmt = update(User).where(User.id == user.id).values(updated_at=datetime.utcnow())
            await session.execute(stmt)
            await session.commit()
        except Exception as e:
            await session.rollback()
            logging.error(f"Error updating last login for user {user.id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not update last login.",
            )
            

# AWS S3 Configuration
AWS_ACCESS_KEY_ID = Config.AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY = Config.AWS_SECRET_ACCESS_KEY
AWS_STORAGE_BUCKET_NAME = Config.AWS_STORAGE_BUCKET_NAME
AWS_REGION = Config.AWS_REGION

s3 = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION,
)


async def upload_image_to_s3(file: UploadFile) -> Optional[str]:
    """
    Uploads an image to AWS S3 and returns the URL.
    """
    try:
        # Generate a unique filename to avoid collisions
        file_name = f"{uuid4()}-{file.filename}"

        # Upload the file to S3
        s3.upload_fileobj(
            file.file,
            AWS_STORAGE_BUCKET_NAME,
            file_name,
            ExtraArgs={"ACL": "public-read"},
        )
        # Construct the full URL to the uploaded file
        image_url = f"https://{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{file_name}"
        return image_url
    except Exception as e:
        logging.error(f"Error uploading to S3: {e}")
        return None