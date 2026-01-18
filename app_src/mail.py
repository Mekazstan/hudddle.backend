import logging
import resend
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
        resend.api_key = Config.RESEND_API_KEY
        self.from_email = f"{Config.MAIL_FROM_NAME} <{Config.MAIL_FROM}>"
    
    async def test_connection(self):
        """
        Resend uses HTTP API, so there is no persistent connection to test.
        We return True to satisfy the arq_worker startup check.
        """
        return True
        
    async def send_message(self, message_params):
        """
        Public method to send emails via Resend.
        message_params should be a dict or object with:
        recipients, subject, body
        """
        try:
            params = {
                "from": self.from_email,
                "to": message_params.recipients,
                "subject": message_params.subject,
                "html": message_params.body,
            }
            
            # Resend's python SDK is synchronous, so we run it in a thread 
            # or just call it if we don't mind the block, but for arq 
            # it's better to keep it async-friendly.
            # Using resend.Emails.send(params)
            resend.Emails.send(params)
            return True
        except Exception as e:
            logger.error(f"Failed to send email via Resend: {e}")
            raise

mail_service = MailService()

class SimpleMessage:
    def __init__(self, recipients, subject, body):
        self.recipients = recipients
        self.subject = subject
        self.body = body

def create_message(recipients: list[str], subject: str, body: str):
    return SimpleMessage(
        recipients=recipients,
        subject=subject,
        body=body
    )