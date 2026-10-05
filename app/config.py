"""
Configuration settings for the Enterprise Mass Email System.
Provides environment-aware configuration for SQLite database paths, EML archive vault,
tracking URLs, default rate limits, and security settings.
"""

import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


# Base project directory
BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file() -> None:
    """Load key-value pairs from .env file into os.environ if present."""
    env_file = BASE_DIR / ".env"
    if env_file.is_file():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except Exception:
            pass


load_env_file()


class Settings(BaseModel):
    """Application configuration schema."""
    
    # Application Info
    APP_NAME: str = Field(
        default=os.getenv("APP_NAME", "Bitmail Enterprise Mass Email & Storage Platform")
    )
    APP_ENV: str = Field(
        default=os.getenv("APP_ENV", "development")
    )
    DEBUG: bool = Field(
        default=os.getenv("DEBUG", "true").strip().lower() in ("true", "1", "yes")
    )
    LOG_LEVEL: str = Field(
        default=os.getenv("LOG_LEVEL", "INFO")
    )
    
    # Server & Tracking
    HOST: str = Field(
        default=os.getenv("HOST", "0.0.0.0")
    )
    PORT: int = Field(
        default=int(os.getenv("PORT", "8000"))
    )
    TRACKING_BASE_URL: str = Field(
        default=os.getenv("TRACKING_BASE_URL", "http://localhost:8000")
    )
    
    # Database Settings
    DATA_DIR: Path = Field(
        default=Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
    )
    DATABASE_PATH: Path = Field(
        default=Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "mass_email.db")))
    )
    
    # SQLite WAL & Performance Pragmas
    SQLITE_BUSY_TIMEOUT_MS: int = Field(default=5000)
    SQLITE_CACHE_SIZE_KB: int = Field(default=-64000)  # ~64MB cache
    SQLITE_MMAP_SIZE_BYTES: int = Field(default=268435456)  # 256MB mmap
    SQLITE_WAL_AUTODISKCHECKPOINT: int = Field(default=1000)
    
    # Sent Email Storage Vault (Raw EML files & archive storage)
    STORAGE_DIR: Path = Field(
        default=Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "storage")))
    )
    EML_STORAGE_DIR: Path = Field(
        default=Path(os.getenv("EML_STORAGE_DIR", str(BASE_DIR / "storage" / "eml")))
    )
    EML_ARCHIVE_DIR: Path = Field(
        default=Path(os.getenv("EML_ARCHIVE_DIR", str(BASE_DIR / "data" / "eml_archive")))
    )
    STORE_RAW_EML_FILES: bool = Field(default=True)
    
    # Email Delivery & Rate Limiting Defaults
    DEFAULT_RATE_LIMIT_PER_SEC: int = Field(default=25)
    DEFAULT_DAILY_QUOTA: int = Field(default=50000)
    DEFAULT_BATCH_SIZE: int = Field(default=100)
    MAX_CONCURRENT_SENDS: int = Field(default=10)
    MAX_RETRY_ATTEMPTS: int = Field(default=3)
    RETRY_BACKOFF_BASE_SECONDS: int = Field(default=5)
    SMTP_CONNECTION_TIMEOUT_SECONDS: int = Field(default=30)
    
    # Security & Tokens
    SECRET_KEY: str = Field(
        default=os.getenv("SECRET_KEY", "insecure-dev-only-secret-key-change-in-production")
    )
    TRACKING_TOKEN_SALT: str = Field(default="bitmail-tracking-salt")
    UNSUBSCRIBE_TOKEN_SALT: str = Field(default="bitmail-unsubscribe-salt")
    DEFAULT_ADMIN_EMAIL: str = Field(default=os.getenv("DEFAULT_ADMIN_EMAIL", "admin@bitmail.com"))
    DEFAULT_ADMIN_USERNAME: str = Field(default=os.getenv("DEFAULT_ADMIN_USERNAME", "admin"))
    DEFAULT_ADMIN_PASSWORD: str = Field(default=os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123"))
    SESSION_EXPIRE_DAYS: int = Field(default=30)
    
    # Default Sender Settings
    DEFAULT_SENDER_NAME: str = Field(default=os.getenv("DEFAULT_SENDER_NAME", "Bitnade"))
    DEFAULT_SENDER_EMAIL: str = Field(default=os.getenv("DEFAULT_SENDER_EMAIL", "contact@bitnade.com"))
    
    # Company / Compliance Information
    COMPANY_NAME: str = Field(default=os.getenv("COMPANY_NAME", "Bitnade"))
    COMPANY_ADDRESS: str = Field(default=os.getenv("COMPANY_ADDRESS", "Bitnade, San Francisco, CA"))
    PRIVACY_POLICY_URL: str = Field(default=os.getenv("PRIVACY_POLICY_URL", "https://bitnade.com/privacy"))
    
    def ensure_directories(self) -> None:
        """Create necessary data and storage directories if they do not exist."""
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if self.DATABASE_PATH.parent:
            self.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self.EML_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self.EML_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    def validate_security_config(self) -> None:
        """Enforce strict secret hygiene in production and warn in development."""
        import logging
        log = logging.getLogger("bitmail.security")
        is_prod = (self.APP_ENV or "").strip().lower() == "production"

        INSECURE_SECRETS = (
            "insecure-dev-only-secret-key-change-in-production",
            "bitmail-vault-secret-key-production-2026",
        )

        if is_prod:
            if not os.getenv("SECRET_KEY") or self.SECRET_KEY in INSECURE_SECRETS:
                raise RuntimeError(
                    "CRITICAL SECURITY CONFIGURATION ERROR: SECRET_KEY must be explicitly set to a strong secret in production."
                )
            if self.DEFAULT_ADMIN_PASSWORD == "admin123":
                raise RuntimeError(
                    "CRITICAL SECURITY CONFIGURATION ERROR: DEFAULT_ADMIN_PASSWORD cannot be 'admin123' in production."
                )
        else:
            if self.SECRET_KEY in INSECURE_SECRETS:
                log.warning(
                    "[SECURITY WARNING] Using static fallback SECRET_KEY. Set SECRET_KEY environment variable for non-development deployments."
                )
            if self.DEFAULT_ADMIN_PASSWORD == "admin123":
                log.warning(
                    "[SECURITY WARNING] Default admin password 'admin123' is active. Set DEFAULT_ADMIN_PASSWORD environment variable to secure the installation."
                )


# Global settings singleton
settings = Settings()
settings.ensure_directories()
settings.validate_security_config()

