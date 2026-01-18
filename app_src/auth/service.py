from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from fastapi import HTTPException, status, UploadFile
from app_src.db.models import LevelCategory, LevelTier, User, UserLevel
from uuid import UUID, uuid4
from .schema import UserCreateModel
from .utils import generate_password_hash
import logging
from app_src.config import Config
from datetime import datetime
import cloudinary
import cloudinary.uploader
import cloudinary.api
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
        try:
            user_data_dict = user_data.model_dump()
            password = user_data_dict.pop("password")
            new_user = User(**user_data_dict)
            new_user.email = user_data.email.lower()
            new_user.password_hash = generate_password_hash(password)
            new_user.role = "user"
            session.add(new_user)
            await session.flush()
            return new_user
        except IntegrityError as e:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User with email {user_data.email} already exists."
            )
        except Exception as e:
            logging.error(f"Error creating user: {str(e)}")
            raise
        
    async def create_level_for_user(self, user_id: UUID, session: AsyncSession):
        # Create a UserLevel entry for each LevelCategory
        levels = [
            UserLevel(
                user_id=user_id,
                level_category=category,
                level_tier=LevelTier.BEGINNER,
                level_points=0
            )
            for category in LevelCategory
        ]
        session.add_all(levels)

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
            

cloudinary.config(
    cloud_name=Config.CLOUDINARY_CLOUD_NAME,
    api_key=Config.CLOUDINARY_API_KEY,
    api_secret=Config.CLOUDINARY_API_SECRET
)


async def upload_image_to_cloudinary(file: UploadFile) -> Optional[str]:
    """
    Uploads an image to Cloudinary and returns the URL.
    """
    try:
        MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
        if file.size > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="File too large")

        # Generate a unique filename to avoid collisions
        unique_filename = f"{uuid4()}-{file.filename.split('.')[0]}"
        
        # Create the Cloudinary public_id with the profile_images folder
        cloudinary_public_id = f"hudddle/profile_images/{unique_filename}"

        try:
            # Read file content
            file.file.seek(0)
            file_content = await file.read()
            
            # Upload the file to Cloudinary
            upload_result = cloudinary.uploader.upload(
                file_content,
                public_id=cloudinary_public_id,
                resource_type="image",
                format="auto",
                quality="auto",
                fetch_format="auto",
                tags=["profile_image"],
                context={
                    'original_filename': file.filename,
                    'upload_timestamp': datetime.utcnow().isoformat()
                }
            )
            
            # Return the secure URL
            return upload_result['secure_url']
            
        except Exception as upload_error:
            logging.error(f"Cloudinary upload error: {upload_error}")
            raise upload_error
            
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error uploading to Cloudinary: {e}")
        return None
    
async def delete_profile_image(public_id: str) -> bool:
    """
    Deletes a profile image from Cloudinary using its public_id.
    """
    try:
        result = cloudinary.uploader.destroy(
            public_id,
            resource_type="image"
        )
        
        if result.get('result') == 'ok':
            logging.info(f"Deleted profile image: {public_id} from Cloudinary")
            return True
        else:
            logging.warning(f"Cloudinary profile image deletion returned: {result}")
            return False
            
    except Exception as e:
        logging.error(f"Error deleting profile image from Cloudinary: {e}")
        return False


def extract_public_id_from_url(cloudinary_url: str) -> Optional[str]:
    """
    Extracts the public_id from a Cloudinary URL for deletion purposes.
    
    Example:
    Input: "https://res.cloudinary.com/your-cloud/image/upload/v123456/hudddle/profile_images/abc-123.jpg"
    Output: "hudddle/profile_images/abc-123"
    """
    try:
        from urllib.parse import urlparse
        import re
        
        parsed_url = urlparse(cloudinary_url)
        path = parsed_url.path
        
        # Extract public_id from Cloudinary URL pattern
        # Pattern: /image/upload/v{version}/{public_id}.{format}
        match = re.search(r'/image/upload/v\d+/(.+)\.[^.]+$', path)
        if match:
            return match.group(1)
        
        # Fallback pattern without version
        match = re.search(r'/image/upload/(.+)\.[^.]+$', path)
        if match:
            return match.group(1)
            
        return None
        
    except Exception as e:
        logging.error(f"Error extracting public_id from URL: {e}")
        return None
