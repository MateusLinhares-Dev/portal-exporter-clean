from __future__ import annotations
import uuid
from datetime import UTC, datetime
from typing import Any
from sqlalchemy import BigInteger, DateTime, Integer, String, Text, UniqueConstraint, JSON, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from softexpert_exporter.domain.enums import ExtractionStatus, JobStatus
from softexpert_exporter.domain.models import SyncJob, ReportDescriptor, ExportMode

class Base(DeclarativeBase):
    pass

class SyncJobModel(Base):
    __tablename__ = 'sync_jobs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    requested_by: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default='incremental')
    options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    schedule_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    downloaded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)

class ExtractionModel(Base):
    __tablename__ = 'extractions'
    __table_args__ = (UniqueConstraint('source_key', name='uq_extractions_source_key'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    portal_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    tab: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    destination_path: Mapped[str | None] = mapped_column(Text)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

def _job(model: SyncJobModel) -> SyncJob:
    return SyncJob(id=model.id, status=JobStatus(model.status), requested_by=model.requested_by,
                   mode=model.mode, options=model.options or {}, created_at=model.created_at,
                   started_at=model.started_at, finished_at=model.finished_at,
                   heartbeat_at=model.heartbeat_at, attempts=model.attempts,
                   next_attempt_at=model.next_attempt_at, total_records=model.total_records,
                   downloaded=model.downloaded, skipped=model.skipped, failed=model.failed,
                   error_summary=model.error_summary)

class SqlAlchemyJobRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.factory = session_factory

    async def create_job(self, requested_by: str, mode: ExportMode, options: dict[str, Any]) -> SyncJob:
        model = SyncJobModel(id=str(uuid.uuid4()), status=JobStatus.PENDING.value,
                             requested_by=requested_by, mode=mode, options=options,
                             created_at=datetime.now(UTC))
        model.schedule_key = options.get('schedule_key')
        async with self.factory() as session:
            if model.schedule_key:
                statement = insert(SyncJobModel).values(
                    id=model.id, status=model.status, requested_by=model.requested_by,
                    mode=model.mode, options=model.options, schedule_key=model.schedule_key,
                    created_at=model.created_at,
                    attempts=0, total_records=0, downloaded=0, skipped=0, failed=0,
                ).on_conflict_do_nothing(index_elements=[SyncJobModel.schedule_key])
                await session.execute(statement)
                await session.commit()
                existing = await session.scalar(select(SyncJobModel).where(
                    SyncJobModel.schedule_key == model.schedule_key))
                if existing is None:
                    raise RuntimeError('Unable to find scheduled job')
                return _job(existing)
            session.add(model)
            await session.commit()
            await session.refresh(model)
        return _job(model)

    async def get_job(self, job_id: str) -> SyncJob | None:
        async with self.factory() as session:
            result = await session.get(SyncJobModel, job_id)
            return _job(result) if result else None

    async def claim_next_job(self) -> SyncJob | None:
        async with self.factory() as session:
            async with session.begin():
                result = await session.execute(select(SyncJobModel).where(
                    SyncJobModel.status == JobStatus.PENDING.value,
                    or_(SyncJobModel.next_attempt_at.is_(None),
                        SyncJobModel.next_attempt_at <= datetime.now(UTC))
                ).order_by(SyncJobModel.created_at).with_for_update(skip_locked=True).limit(1))
                model = result.scalar_one_or_none()
                if model is None:
                    return None
                now = datetime.now(UTC)
                model.status = JobStatus.RUNNING.value
                model.started_at = model.started_at or now
                model.heartbeat_at = now
                model.attempts += 1
                model.next_attempt_at = None
            await session.refresh(model)
            return _job(model)

    async def heartbeat(self, job_id: str) -> None:
        async with self.factory() as session:
            await session.execute(update(SyncJobModel).where(SyncJobModel.id == job_id)
                                  .values(heartbeat_at=datetime.now(UTC)))
            await session.commit()

    async def complete_job(self, job_id: str, *, total_records: int, downloaded: int,
                           skipped: int, failed: int, error_summary: str | None) -> SyncJob:
        status = JobStatus.SUCCEEDED if failed == 0 else JobStatus.PARTIAL if downloaded or skipped else JobStatus.FAILED
        async with self.factory() as session:
            model = await session.get(SyncJobModel, job_id)
            if model is None: raise LookupError(job_id)
            model.status = status.value
            model.finished_at = datetime.now(UTC)
            model.heartbeat_at = model.finished_at
            model.total_records = total_records
            model.downloaded = downloaded
            model.skipped = skipped
            model.failed = failed
            model.error_summary = error_summary
            model.next_attempt_at = None
            await session.commit()
            await session.refresh(model)
            return _job(model)

    async def fail_job(self, job_id: str, error_summary: str) -> SyncJob:
        async with self.factory() as session:
            model = await session.get(SyncJobModel, job_id)
            if model is None: raise LookupError(job_id)
            model.status = JobStatus.FAILED.value
            model.finished_at = datetime.now(UTC)
            model.heartbeat_at = model.finished_at
            model.error_summary = error_summary[:4000]
            model.next_attempt_at = None
            await session.commit()
            await session.refresh(model)
            return _job(model)

    async def requeue_job(self, job_id: str, *, error_summary: str, next_attempt_at: datetime) -> SyncJob:
        async with self.factory() as session:
            model = await session.get(SyncJobModel, job_id)
            if model is None: raise LookupError(job_id)
            model.status = JobStatus.PENDING.value
            model.heartbeat_at = None
            model.next_attempt_at = next_attempt_at
            model.error_summary = error_summary[:4000]
            await session.commit()
            await session.refresh(model)
            return _job(model)

    async def recover_stale_jobs(self, stale_before: datetime) -> int:
        async with self.factory() as session:
            result = await session.execute(update(SyncJobModel)
                .where(SyncJobModel.status == JobStatus.RUNNING.value,
                       SyncJobModel.heartbeat_at < stale_before)
                .values(status=JobStatus.PENDING.value, heartbeat_at=None, next_attempt_at=None))
            await session.commit()
            return result.rowcount or 0

    async def extracted_path(self, source_key: str) -> str | None:
        async with self.factory() as session:
            result = await session.execute(select(ExtractionModel.destination_path).where(
                ExtractionModel.source_key == source_key,
                ExtractionModel.status == ExtractionStatus.SUCCEEDED.value))
            return result.scalar_one_or_none()

    async def mark_extracted(self, *, job_id: str, source_key: str, descriptor: ReportDescriptor,
                             destination_path: str, checksum_sha256: str, size_bytes: int) -> None:
        await self._upsert(job_id, source_key, descriptor, ExtractionStatus.SUCCEEDED,
                           destination_path, checksum_sha256, size_bytes, None)

    async def mark_extraction_failed(self, *, job_id: str, source_key: str,
                                     descriptor: ReportDescriptor, error_message: str) -> None:
        await self._upsert(job_id, source_key, descriptor, ExtractionStatus.FAILED,
                           None, None, None, error_message[:4000])

    async def _upsert(self, job_id: str, source_key: str, desc: ReportDescriptor,
                      status: ExtractionStatus, path: str | None,
                      checksum: str | None, size: int | None, error: str | None) -> None:
        now = datetime.now(UTC)
        data = dict(job_id=job_id, source_key=source_key, status=status.value,
                    document_id=desc.document_id, source_url=desc.url,
                    portal_path=' / '.join(desc.portal_path),
                    file_name=(desc.code or desc.document_id) + '.pdf',
                    tab=desc.tab, unit=desc.unit, destination_path=path,
                    checksum_sha256=checksum, size_bytes=size, error_message=error,
                    created_at=now, updated_at=now)
        stmt = insert(ExtractionModel).values(**data)
        update_fields = {k:v for k,v in data.items() if k not in {'source_key', 'created_at'}}
        stmt = stmt.on_conflict_do_update(
            index_elements=[ExtractionModel.source_key], set_=update_fields,
            where=(ExtractionModel.status != ExtractionStatus.SUCCEEDED.value)
              if status is ExtractionStatus.FAILED else None)
        async with self.factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def healthcheck(self) -> bool:
        try:
            async with self.factory() as session:
                await session.execute(text('SELECT 1'))
            return True
        except Exception:
            return False

def create_session_factory(url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(url, pool_pre_ping=True, pool_recycle=1800)
    return async_sessionmaker(engine, expire_on_commit=False)
