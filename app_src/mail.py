import logging
from fastapi_mail import FastMail, ConnectionConfig, MessageSchema
from app_src.config import Config
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

logger = logging.getLogger(__name__)

class MailService:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance
    
    def _initialize(self):
        self.config = ConnectionConfig(
            MAIL_USERNAME=Config.MAIL_USERNAME,
            MAIL_PASSWORD=Config.MAIL_PASSWORD,
            MAIL_FROM=Config.MAIL_FROM,
            MAIL_PORT=Config.MAIL_PORT,
            MAIL_SERVER=Config.MAIL_SERVER,
            MAIL_FROM_NAME=Config.MAIL_FROM_NAME,
            MAIL_STARTTLS=True,
            MAIL_SSL_TLS=False,
            USE_CREDENTIALS=True,
            VALIDATE_CERTS=True,
            TIMEOUT=10
        )
        self.mail = FastMail(self.config)
    
    async def test_connection(self):
        """Test the SMTP connection by starting and closing a connection"""
        try:
            # Create a test message
            message = MessageSchema(
                recipients=["test@example.com"],
                subject="Connection Test",
                body="<p>Test</p>",
                subtype="html"
            )
            
            # This will implicitly test the connection
            await self.mail.send_message(message)
            return True
        except Exception as e:
            logger.error(f"Mail connection test failed: {e}")
            return False
        
    async def send_message(self, message):
        """Public method to send emails"""
        try:
            await self.mail.send_message(message)
            return True
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            raise

mail_service = MailService()

def create_message(recipients: list[str], subject: str, body: str):
    return MessageSchema(
        recipients=recipients,
        subject=subject,
        body=body,
        subtype="html"
    )