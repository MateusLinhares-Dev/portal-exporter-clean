"""Verifica PDF compacto, indexacao completa, protecao de segredos e escala."""
import csv
from datetime import UTC, datetime
from pathlib import Path

from softexpert_exporter.domain.enums import JobStatus
from softexpert_exporter.domain.models import SyncJob
from softexpert_exporter.infrastructure.execution_report import (
    build_pdf, protected_url, write_audit_csvs,
)


def job(count=0):
    now = datetime.now(UTC)
    return SyncJob(id='job-001', status=JobStatus.SUCCEEDED, requested_by='teste',
                   mode='test', options={}, created_at=now, started_at=now,
                   finished_at=now, attempts=1, total_records=count,
                   downloaded=count, skipped=0, failed=0)


def event(n):
    return dict(kind='document', status='downloaded', attempt=1, tab='Obras',
                portal_path='Checklist / Obras', document_id=str(n),
                document_code=f'DOC-{n:05}', document_date='29/10/2026',
                unit='Matriz', source_url='https://example.org/pdf?token=SECRETO&id=1',
                file_name=f'DOC-{n:05}.pdf',
                destination_path=f'Obras/DOC-{n:05}.pdf',
                size_bytes=1234, checksum_sha256='f'*64, message='',
                occurred_at=datetime.now(UTC))


def test_mask_protected_url():
    output = protected_url('https://bob:pass@example.org/x?id=123&token=segredo&password=outra#fragmento')
    assert 'id=123' in output
    assert 'segredo' not in output and 'outra' not in output
    assert 'bob' not in output and 'fragmento' not in output


def test_reports_no_events(tmp_path):
    path = tmp_path/'vazio.pdf'
    build_pdf(path, job=job(), events=[], storage_root=tmp_path)
    assert path.read_bytes().startswith(b'%PDF-')


def test_large_audit_stays_compact_and_complete(tmp_path):
    events = [event(n) for n in range(10000)]
    documents = tmp_path/'documentos.csv'
    navigation = tmp_path/'navegacao.csv'
    pdf = tmp_path/'relatorio.pdf'
    write_audit_csvs(documents,navigation,job=job(10000),events=events,storage_root=tmp_path)
    build_pdf(pdf, job=job(10000),events=events,storage_root=tmp_path,
              documents_csv_name=documents.name,navigation_csv_name=navigation.name)
    assert pdf.read_bytes().startswith(b'%PDF-')
    assert pdf.stat().st_size < 400_000
    with documents.open('r',encoding='utf-8-sig',newline='') as f:
        reader = csv.DictReader(f,delimiter=';')
        rows = list(reader)
    assert len(rows)==10000
    assert rows[-1]['Nome arquivo']=='DOC-09999.pdf'
    assert rows[0]['Caminho completo'].endswith('Obras/DOC-00000.pdf')
    assert 'SECRETO' not in documents.read_text(encoding='utf-8-sig')
    assert navigation.exists()


def test_publishing_persists_pdf_and_both_csvs(tmp_path):
    import asyncio
    import shutil
    from types import SimpleNamespace
    from softexpert_exporter.infrastructure.execution_report import publish_job_pdf

    class Repo:
        async def list_audit_events(self, job_id):
            assert job_id == 'job-001'
            return [event(1), event(2)]
        async def record_report_result(self, job_id, **kwargs):
            self.result = kwargs

    class Storage:
        async def store(self, source, relative, digest, size):
            target=tmp_path/'out'/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
            assert target.stat().st_size==size
            return SimpleNamespace(relative_path=relative)

    repo=Repo()
    settings=SimpleNamespace(TEMP_DIR=tmp_path/'tmp', NETWORK_SHARE_ROOT=tmp_path/'out',
                             PORTAL_ENTRY_URL='https://exemplo.invalid/portal')
    path=asyncio.run(publish_job_pdf(job=job(2),repository=repo,
                                    storage=Storage(),settings=settings))
    assert path.endswith('.pdf')
    assert repo.result['path']==path
    folder=(tmp_path/'out'/path).parent
    assert len(list(folder.glob('*.pdf')))==1
    assert len(list(folder.glob('*.csv')))==2
