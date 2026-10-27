from __future__ import annotations
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from softexpert_exporter.application.portal import PortalScrapeUseCase
from softexpert_exporter.config import Settings, get_settings
from softexpert_exporter.infrastructure.db import SqlAlchemyJobRepository, create_session_factory
from softexpert_exporter.infrastructure.storage import NetworkShareStorage

@dataclass(slots=True)
class Container:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    repository: SqlAlchemyJobRepository
    storage: NetworkShareStorage
    sync_use_case: PortalScrapeUseCase

    async def close(self) -> None:
        await self.session_factory.kw['bind'].dispose()

def build_container() -> Container:
    settings = get_settings()
    factory = create_session_factory(settings.DATABASE_URL)
    repository = SqlAlchemyJobRepository(factory)
    storage = NetworkShareStorage(settings.NETWORK_SHARE_ROOT,
                                 require_sentinel=settings.NETWORK_SHARE_REQUIRE_SENTINEL,
                                 sentinel_name=settings.NETWORK_SHARE_SENTINEL_NAME)
    return Container(settings, factory, repository, storage,
                     PortalScrapeUseCase(settings=settings, storage=storage, repository=repository))
