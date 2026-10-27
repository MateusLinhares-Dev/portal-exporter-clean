from __future__ import annotations
import hmac
from datetime import UTC, date, datetime
from typing import Annotated, cast, Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from softexpert_exporter.bootstrap import Container
from softexpert_exporter.domain.models import SyncJob
from softexpert_exporter.date_windows import previous_six_months
router = APIRouter()

class SyncRequest(BaseModel):
    requested_by: str = Field(default='manual', min_length=1, max_length=120)
    mode: Literal['test', 'historical', 'incremental'] = 'test'
    # Somente no modo teste; 0 em qualquer limite significa ilimitado nos demais modos.
    max_reports: int | None = Field(default=None, ge=0, le=1000000)
    max_portals: int | None = Field(default=None, ge=0, le=1000000)
    max_depth: int | None = Field(default=None, ge=0, le=16)
    cutoff_date: date | None = None
    window_start: date | None = None
    window_end: date | None = None
    force: bool = False

class SyncResponse(BaseModel):
    job_id: str
    status: str
    mode: str

class JobResponse(BaseModel):
    id: str
    status: str
    requested_by: str
    mode: str
    options: dict
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    attempts: int
    next_attempt_at: datetime | None
    total_records: int
    downloaded: int
    skipped: int
    failed: int
    error_summary: str | None

def container_from(request: Request) -> Container:
    return cast(Container, request.app.state.container)

async def require_api_key(container: Annotated[Container, Depends(container_from)],
                          x_api_key: Annotated[str | None, Header()] = None) -> None:
    configured = container.settings.APP_API_KEY
    if configured and (x_api_key is None or not hmac.compare_digest(x_api_key, configured)):
        raise HTTPException(status_code=401, detail='Invalid API key')

def to_response(job: SyncJob) -> JobResponse:
    return JobResponse(**{key: getattr(job, key) for key in JobResponse.model_fields})

@router.post('/api/v1/sync', response_model=SyncResponse, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(require_api_key)])
async def create_sync(body: SyncRequest, container: Annotated[Container, Depends(container_from)]) -> SyncResponse:
    options = body.model_dump(exclude={'requested_by', 'mode'}, exclude_none=True)
    if body.mode == 'test':
        options.setdefault('max_reports', 5)
        options.setdefault('max_portals', 3)
        options.setdefault('max_depth', 3)
        # Evita disparar extração integral acidental via modo teste
        if options['max_reports'] == 0 or options['max_portals'] == 0:
            raise HTTPException(status_code=422, detail='Test mode requires positive max_reports and max_portals')
    if body.mode == 'historical':
        cutoff = body.cutoff_date or container.settings.HISTORICAL_CUTOFF_DATE
        if cutoff is None:
            raise HTTPException(status_code=422,
                detail='Historical job requires cutoff_date (approval date) or HISTORICAL_CUTOFF_DATE')
        if cutoff > datetime.now(UTC).date():
            raise HTTPException(status_code=422, detail='cutoff_date cannot be in the future')
        options['cutoff_date'] = cutoff.isoformat()
        options.pop('window_start', None)
        options.pop('window_end', None)
    elif body.mode == 'incremental':
        end = body.window_end or datetime.now(UTC).date()
        start = body.window_start or previous_six_months(end)
        if start >= end or end > datetime.now(UTC).date():
            raise HTTPException(status_code=422, detail='Require window_start < window_end <= today')
        options['window_start'] = start.isoformat()
        options['window_end'] = end.isoformat()
        options.pop('cutoff_date', None)
    else:
        for item in ('window_start', 'window_end', 'cutoff_date'):
            options.pop(item, None)
    job = await container.repository.create_job(body.requested_by, body.mode, options)
    return SyncResponse(job_id=job.id, status=job.status.value, mode=job.mode)

@router.get('/api/v1/sync/{job_id}', response_model=JobResponse, dependencies=[Depends(require_api_key)])
async def get_sync(job_id: str, container: Annotated[Container, Depends(container_from)]) -> JobResponse:
    job = await container.repository.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    return to_response(job)

@router.get('/health/live')
async def live() -> dict[str, str]:
    return {'status': 'ok'}

@router.get('/health/ready')
async def ready(container: Annotated[Container, Depends(container_from)]) -> dict[str, object]:
    db = await container.repository.healthcheck()
    share = await container.storage.is_ready()
    if not (db and share):
        raise HTTPException(status_code=503, detail={'database': db, 'network_share': share})
    return {'status': 'ready', 'database': db, 'network_share': share}
