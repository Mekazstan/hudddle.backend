from pydantic_settings import BaseSettings, SettingsConfigDict
from urllib.parse import quote_plus

class Settings(BaseSettings):
    MONGO_USERNAME: str
    MONGO_PASSWORD: str
    MONGO_CLUSTER: str
    MONGO_DB_NAME: str
    DATABASE_URL: str
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str
    MAIL_USERNAME: str
    MAIL_PASSWORD: str
    MAIL_FROM: str
    MAIL_PORT: int
    MAIL_SERVER: str
    MAIL_FROM_NAME: str
    MAIL_STARTTLS: bool = True
    MAIL_SSL_TLS: bool = False
    USE_CREDENTIALS: bool = True
    VALIDATE_CERTS: bool = True
    DOMAIN: str

    # Dynamically compute MONGO_URI after the class is instantiated
    @property
    def MONGO_URI(self) -> str:
        escaped_username = quote_plus(self.MONGO_USERNAME)
        escaped_password = quote_plus(self.MONGO_PASSWORD)
        return (
            f"mongodb+srv://{escaped_username}:{escaped_password}@{self.MONGO_CLUSTER}.mjj7d.mongodb.net/?retryWrites=true&w=majority&appName={self.MONGO_CLUSTER}"
        )

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

# Instantiate the Config object
Config = Settings()
