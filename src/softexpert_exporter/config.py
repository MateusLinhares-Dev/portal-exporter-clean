from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', case_sensitive=True, extra='ignore')
    APP_ENV: str = 'local'
    APP_API_KEY: str = ''
    LOG_LEVEL: str = 'INFO'
    PORTAL_ENTRY_URL: str
    PORTAL_USERNAME: str = ''
    PORTAL_PASSWORD: str = ''
    PORTAL_ALLOWED_REPORT_ORIGINS: str
    PORTAL_ROOT_TABS: str = ''
    PORTAL_TIMEOUT_MS: int = Field(default=30000, ge=1000, le=120000)
    PORTAL_MAX_PDF_BYTES: int = Field(default=104857600, ge=1024)
    HISTORICAL_CUTOFF_DATE: date | None = None
    DATABASE_URL: str
    NETWORK_SHARE_ROOT: Path = Path('/mnt/client-share')
    NETWORK_SHARE_REQUIRE_SENTINEL: bool = False
    NETWORK_SHARE_SENTINEL_NAME: str = 'softexpert-exporter-share.ready'
    TEMP_DIR: Path = Path('/tmp/softexpert-exporter')
    WORKER_POLL_SECONDS: float = Field(default=5, gt=0)
    WORKER_STALE_AFTER_SECONDS: int = Field(default=3600, ge=300)
    WORKER_JOB_MAX_ATTEMPTS: int = Field(default=4, ge=1)
    WORKER_JOB_RETRY_SECONDS: int = Field(default=60, ge=1)
    SCHEDULER_ENABLED: bool = False
    SCHEDULER_CRON: str = '0 2 1 1,7 *'
    SCHEDULER_TIMEZONE: str = 'America/Sao_Paulo'

    @field_validator('HISTORICAL_CUTOFF_DATE', mode='before')
    @classmethod
    def empty_cutoff_date_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode='after')
    def validate_runtime(self) -> 'Settings':
        from softexpert_exporter.infrastructure.portal_scraper import origin
        source_origin = origin(self.PORTAL_ENTRY_URL)
        origins = self.report_origins
        if source_origin not in origins:
            raise ValueError('PORTAL_ALLOWED_REPORT_ORIGINS must include PORTAL_ENTRY_URL origin')
        if self.APP_ENV == 'production':
            if not self.APP_API_KEY or self.APP_API_KEY == 'change-me':
                raise ValueError('Set a strong APP_API_KEY in production')
            if not self.NETWORK_SHARE_REQUIRE_SENTINEL:
                raise ValueError('Require network-share sentinel in production')
        return self

    @property
    def report_origins(self) -> set[str]:
        from softexpert_exporter.infrastructure.portal_scraper import origin
        return {origin(u.strip()) for u in self.PORTAL_ALLOWED_REPORT_ORIGINS.split(',') if u.strip()}

    @property
    def root_tabs(self) -> list[str]:
        return [v.strip() for v in self.PORTAL_ROOT_TABS.split(',') if v.strip()]

    @property
    def portal_origin(self) -> str:
        from softexpert_exporter.infrastructure.portal_scraper import origin
        return origin(self.PORTAL_ENTRY_URL)

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
