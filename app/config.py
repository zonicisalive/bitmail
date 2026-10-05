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


class Settings(BaseModel):
    """Application configuration schema."""
    
    # Application Info
    APP_NAME: str = Field(default="Bitmail Enterprise Mass Email & Storage Platform")
    APP_ENV: str = Field(default="development")
    DEBUG: bool = Field(default=True)
    LOG_LEVEL: str = Field(default="INFO")
    
    # Server & Tracking
    HOST: str = Field(default="0.0.0.0")
    PORT: int = Field(default=8000)
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
        default=os.getenv("SECRET_KEY", "bitmail-vault-secret-key-production-2026")
    )
    TRACKING_TOKEN_SALT: str = Field(default="bitmail-tracking-salt")
    UNSUBSCRIBE_TOKEN_SALT: str = Field(default="bitmail-unsubscribe-salt")
    
    # Default Sender Settings
    DEFAULT_SENDER_NAME: str = Field(default="Bitmail Team")
    DEFAULT_SENDER_EMAIL: str = Field(default="team@bitmail.io")
    
    # Company / Compliance Information
    COMPANY_NAME: str = Field(default="Bitmail Technologies Inc.")
    COMPANY_ADDRESS: str = Field(default="100 Market St, Suite 500, San Francisco, CA 94105")
    PRIVACY_POLICY_URL: str = Field(default="http://localhost:8000/privacy")
    
    def ensure_directories(self) -> None:
        """Create necessary data and storage directories if they do not exist."""
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if self.DATABASE_PATH.parent:
            self.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self.EML_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self.EML_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


# Global settings singleton
settings = Settings()
settings.ensure_directories()

