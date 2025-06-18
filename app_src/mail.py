import logging
from fastapi_mail import FastMail, ConnectionConfig, MessageSchema
from app_src.config import Config
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def get_mail_config():
    return ConnectionConfig(
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

_mail_instance = None

def get_mail():
    global _mail_instance
    if _mail_instance is None:
        _mail_instance = FastMail(get_mail_config())
    return _mail_instance


def create_message(recipients: list[str], subject: str, body: str):

    message = MessageSchema(
        recipients=recipients,
        subject=subject,
        body=body,
        subtype="html"
    )

    return message
    
