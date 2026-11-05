from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
from collections import Counter, defaultdict
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)

from softexpert_exporter.config import Settings
from softexpert_exporter.domain.models import SyncJob
from softexpert_exporter.infrastructure.storage import safe_relative_path

TZ = ZoneInfo('America/Sao_Paulo')
RED = colors.HexColor('#E7353F')
INK = colors.HexColor('#242C35')
MUTED = colors.HexColor('#65717C')
PALE = colors.HexColor('#F4F6F8')
LINE = colors.HexColor('#E0E5EA')
GREEN = colors.HexColor('#087C59')
AMBER = colors.HexColor('#A86A16')

STATUS = {
    'succeeded': 'CONCLUÍDA', 'partial': 'PARCIAL', 'failed': 'FALHOU',
    'downloaded': 'BAIXADO', 'skipped': 'JÁ EXISTIA', 'out_of_period': 'FORA DO PERÍODO',
    'missing_date': 'DATA INVÁLIDA', 'validated': 'VALIDADO SEM GRAVAR', 'visited': 'VISITADO',
    'navigation_failed': 'FALHA DE NAVEGAÇÃO', 'pagination': 'PAGINAÇÃO',
    'issue': 'ALERTA', 'error': 'ERRO',
}
SENSITIVE_KEYS = ('token', 'password', 'passwd', 'secret', 'session', 'apikey',
                  'api_key', 'access_key', 'auth', 'jwt', 'key', 'credential')
DOCUMENT_FIELDS = [
    'Job ID', 'Tentativa', 'Data/hora (Brasília)', 'Situação', 'Aba',
    'Portal / subportal', 'ID documento', 'Código documento', 'Data documento',
    'Unidade', 'Nome arquivo', 'Caminho relativo', 'Caminho completo',
    'Caminho planejado (não gravado)', 'Tamanho (bytes)', 'SHA-256',
    'URL origem (protegida)', 'Observação / erro',
]
NAV_FIELDS = ['Job ID', 'Tentativa', 'Data/hora (Brasília)', 'Tipo',
              'Situação', 'Aba', 'Portal / subportal', 'Descrição']


def when(dt: datetime | None) -> str:
    if dt is None:
        return 'Não registrado'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(TZ).strftime('%d/%m/%Y %H:%M:%S')


def protected_url(url: str) -> str:
    """Remove userinfo, fragment e parametros sensiveis antes da exportacao."""
    if not url:
        return ''
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname:
            return '[URL inválida]'
        netloc = p.hostname + (f':{p.port}' if p.port else '')
        safe_query = [
            (k, '[PROTEGIDO]' if any(x in k.lower() for x in SENSITIVE_KEYS) else v)
            for k, v in parse_qsl(p.query, keep_blank_values=True)
        ]
        return urlunsplit((p.scheme, netloc, p.path, urlencode(safe_query), ''))
    except (ValueError, TypeError):
        return '[URL inválida]'


def safe_csv(value: object) -> str:
    """Evita formula injection ao abrir CSV gerado com nomes de documentos no Excel."""
    raw = '' if value is None else str(value)
    if raw.lstrip().startswith(('=', '+', '-', '@')) or raw.startswith(('\t', '\r', '\n')):
        return "'" + raw
    return raw


def safe_path(root: Path, relative: str | None) -> str:
    if not relative:
        return ''
    try:
        return str(root / safe_relative_path(relative))
    except ValueError:
        return '[caminho inválido]'


def write_audit_csvs(documents_path: Path, navigation_path: Path, *,
                     job: SyncJob, events: list[dict], storage_root: Path) -> None:
    """Inventário sem truncar eventos; CSVs UTF-8 BOM, separador ; para Excel BR."""
    documents_path.parent.mkdir(parents=True, exist_ok=True)
    navigation_path.parent.mkdir(parents=True, exist_ok=True)
    with documents_path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f, delimiter=';', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(DOCUMENT_FIELDS)
        for e in events:
            if e['kind'] != 'document':
                continue
            saved = e['status'] in ('downloaded', 'skipped')
            relative = e.get('destination_path') or ''
            writer.writerow([safe_csv(v) for v in (
                job.id, e['attempt'], when(e.get('occurred_at')),
                STATUS.get(e['status'], e['status']), e['tab'], e['portal_path'],
                e['document_id'], e['document_code'], e['document_date'], e['unit'],
                e['file_name'], relative if saved else '',
                safe_path(storage_root, relative) if saved else '',
                safe_path(storage_root, relative) if not saved else '',
                e['size_bytes'], e['checksum_sha256'], protected_url(e['source_url']),
                e['message'],
            )])
    with navigation_path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(NAV_FIELDS)
        for e in events:
            if e['kind'] == 'document':
                continue
            writer.writerow([safe_csv(v) for v in (
                job.id, e['attempt'], when(e.get('occurred_at')),
                e['kind'], STATUS.get(e['status'], e['status']),
                e['tab'], e['portal_path'], e['message'],
            )])


def _short(text: object, limit: int = 68) -> str:
    s = str(text or '').replace('\r', ' ').replace('\n', ' ').strip()
    return s if len(s) <= limit else s[:limit - 3] + '...'


def _p(value: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(value) if value is not None else '-') or '-', style)


def _table(rows, widths, *, header: bool = False, font: int = 7,
           top_bottom: int = 5) -> Table:
    t = Table(rows, colWidths=widths, hAlign='LEFT', repeatRows=1 if header else 0)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), INK if header else PALE),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white if header else INK),
        ('ROWBACKGROUNDS', (0, 1 if header else 0), (-1, -1), [colors.white, PALE]),
        ('LINEBELOW', (0, -1), (-1, -1), .5, LINE),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), top_bottom),
        ('BOTTOMPADDING', (0, 0), (-1, -1), top_bottom),
    ]))
    return t


def build_pdf(path: Path, *, job: SyncJob, events: list[dict], storage_root: Path,
              portal_url: str = '', demo: bool = False,
              documents_csv_name: str = '', navigation_csv_name: str = '') -> None:
    """PDF gerencial curto; TODOS os registros ficam nos arquivos CSV referenciados."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pagew, _ = A4
    doc = SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=1.45*cm, rightMargin=1.45*cm,
        topMargin=1.25*cm, bottomMargin=1.5*cm,
        title=f'Falcão Bauer | Auditoria da extração {job.id}',
        author='SoftExpert Portal Exporter',
    )
    w = pagew - doc.leftMargin - doc.rightMargin
    font = 'Helvetica'
    styles = {
        'title': ParagraphStyle('FTitle', fontName='Helvetica-Bold', fontSize=17,
                                leading=21, textColor=INK, spaceAfter=4),
        'sub': ParagraphStyle('FSub', fontName=font, fontSize=8,
                              leading=11, textColor=MUTED),
        'section': ParagraphStyle('FSection', fontName='Helvetica-Bold', fontSize=11,
                                  leading=14, textColor=INK, spaceBefore=15, spaceAfter=7),
        'body': ParagraphStyle('FBody', fontName=font, fontSize=8.1,
                               leading=12, textColor=INK, spaceAfter=4),
        'muted': ParagraphStyle('FMuted', fontName=font, fontSize=7.3,
                                leading=10.6, textColor=MUTED),
        'cell': ParagraphStyle('FCell', fontName=font, fontSize=7.2,
                               leading=9.2, textColor=INK, splitLongWords=1),
        'head': ParagraphStyle('FHead', fontName='Helvetica-Bold', fontSize=7,
                               leading=9.1, textColor=colors.white),
        'metricvalue': ParagraphStyle('FMetricValue', fontName='Helvetica-Bold', fontSize=16,
                                      leading=21, textColor=INK, alignment=TA_CENTER),
        'metriclabel': ParagraphStyle('FMetricLabel', fontName=font, fontSize=7.3,
                                      leading=10, textColor=MUTED, alignment=TA_CENTER),
    }
    story = []
    logo = Path(__file__).resolve().parents[1] / 'assets' / 'falcao_bauer.png'
    title = [
        Paragraph('RELATÓRIO DE EXTRAÇÃO', styles['title']),
        Paragraph('SoftExpert Portal Exporter  |  Auditoria da execução', styles['sub']),
    ]
    if logo.is_file():
        img = Image(str(logo), width=2.7*cm, height=2.7*cm)
        hdr = Table([[img, title]], colWidths=[3.15*cm, w-3.15*cm])
        hdr.setStyle(TableStyle([('VALIGN', (0,0),(-1,-1),'MIDDLE'),
                                 ('LEFTPADDING',(0,0),(-1,-1),0),
                                 ('RIGHTPADDING',(0,0),(-1,-1),0)]))
        story.append(hdr)
    else:
        story.extend(title)
    story.append(HRFlowable(width='100%', thickness=2, color=RED,
                            spaceBefore=5, spaceAfter=12))

    status_name = STATUS.get(job.status.value, job.status.value.upper())
    label = f'ESTADO: {status_name}'
    if demo:
        label = 'AMOSTRA ILUSTRATIVA - DADOS FICTÍCIOS / SEM EXTRAÇÃO REAL'
    story.append(Paragraph(f'<b>{escape(label)}</b>', styles['body']))

    started = job.started_at
    finished = job.finished_at
    duration = '-'
    if started and finished:
        start_utc = started.replace(tzinfo=UTC) if started.tzinfo is None else started
        end_utc = finished.replace(tzinfo=UTC) if finished.tzinfo is None else finished
        secs = max(0, int((end_utc - start_utc).total_seconds()))
        duration = f'{secs // 3600:02}:{secs % 3600 // 60:02}:{secs % 60:02}'
    meta = [
        ('Execução', job.id), ('Responsável', job.requested_by),
        ('Início', when(job.started_at)), ('Conclusão', when(job.finished_at)),
        ('Modo', job.mode), ('Duração', duration),
        ('Tentativas', job.attempts), ('Data do relatório', when(datetime.now(UTC))),
    ]
    meta_rows = []
    for i in range(0, len(meta), 2):
        cells = []
        for key, value in meta[i:i+2]:
            cells.append(Paragraph(f'<font color="#65717C">{escape(key)}</font>  '
                                   f'<b>{escape(_short(value, 52))}</b>', styles['body']))
        meta_rows.append(cells)
    story.append(_table(meta_rows, [w/2, w/2], top_bottom=4))

    docs = [e for e in events if e['kind'] == 'document']
    nav = [e for e in events if e['kind'] == 'navigation']
    issues = [e for e in events if e['kind'] == 'issue']
    final_docs = [e for e in docs if e['attempt'] == job.attempts]
    # Contadores de job refletem a execucao consolidada, sem somar tentativas repetidas.
    counts = Counter(e['status'] for e in final_docs)
    visited_tabs = {e['tab'] for e in nav if e['status'] == 'visited' and e['tab']}
    visited_portals = {(e['tab'], e['portal_path']) for e in nav
                       if e['status'] == 'visited' and e['portal_path']}
    story.append(Paragraph('01  |  PAINEL EXECUTIVO', styles['section']))
    metrics = [
        (job.total_records, 'Registros encontrados'), (job.downloaded, 'PDFs baixados'),
        (job.skipped, 'Já existentes'), (job.failed, 'Falhas registradas'),
        (counts['out_of_period'], 'Fora do período'),
        (counts['missing_date'], 'Datas inválidas'),
        (len(visited_tabs), 'Abas acessadas'), (len(visited_portals), 'Portais acessados'),
    ]
    tiles = []
    for i in range(0, len(metrics), 4):
        tiles.append([
            [Paragraph(f'{metrics[j][0]:,}'.replace(',', '.'), styles['metricvalue']),
             Paragraph(metrics[j][1], styles['metriclabel'])]
            for j in range(i, i+4)
        ])
    t = Table(tiles, colWidths=[w/4]*4)
    t.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),PALE),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('LINEBELOW',(0,0),(-1,0),3,colors.white),
        ('INNERGRID',(0,0),(-1,-1),2,colors.white),
        ('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4),
        ('TOPPADDING',(0,0),(-1,-1),10),('BOTTOMPADDING',(0,0),(-1,-1),10),
    ]))
    story.append(t)
    story.append(Spacer(1, 9))
    story.append(_p('A contagem consolidada é referente ao job concluído. Os CSVs preservam '
                    'o histórico de todas as tentativas, inclusive documentos repetidos.',
                    styles['muted']))

    story.append(Paragraph('02  |  RESULTADOS POR ABA', styles['section']))
    per_tab: dict[str, Counter] = defaultdict(Counter)
    for e in final_docs:
        per_tab[e['tab'] or 'Sem aba'][e['status']] += 1
    for e in nav:
        if e['status'] == 'visited':
            per_tab[e['tab'] or 'Sem aba']['_visited'] += 1
    header = ['Aba', 'Registros', 'Baixados', 'Existentes', 'Falhas*', 'Excluídos']
    widths = [w*.31, w*.135, w*.14, w*.135, w*.13, w*.15]
    rows = [[_p(x, styles['head']) for x in header]]
    per_tab_items = sorted(per_tab.items(), key=lambda kv: (-sum(v for k,v in kv[1].items() if not k.startswith('_')),kv[0]))
    for tab, c in per_tab_items[:24]:
        rows.append([
            _p(_short(tab, 38), styles['cell']),
            _p(sum(v for k,v in c.items() if not k.startswith('_')), styles['cell']),
            _p(c['downloaded'], styles['cell']),
            _p(c['skipped'], styles['cell']),
            _p(c['failed']+c['missing_date'], styles['cell']),
            _p(c['out_of_period'], styles['cell']),
        ])
    if len(per_tab_items) > 24:
        story.append(_p(f'Agrupamento exibindo 24 de {len(per_tab_items)} abas; '
                        'o inventário integral consta nos CSVs.', styles['muted']))
    story.append(_table(rows, widths, header=True, top_bottom=5))
    story.append(_p('* Falhas por documento. Alertas de navegação aparecem abaixo e podem '
                    'integrar a contagem geral de falhas.', styles['muted']))

    story.append(Paragraph('03  |  OCORRÊNCIAS QUE EXIGEM ATENÇÃO', styles['section']))
    problem_events = [e for e in events if (
        (e['kind']=='document' and e['status'] in ('failed','missing_date'))
        or (e['kind']=='navigation' and e['status']=='navigation_failed')
        or e['kind']=='issue')]
    if not problem_events:
        story.append(_p('Nenhuma ocorrência individual registrada na auditoria.', styles['body']))
    else:
        story.append(_p(f'{len(problem_events)} ocorrência(s) registradas; abaixo as primeiras '
                        f'{min(len(problem_events), 14)}. Lista integral disponível nos CSVs.',
                        styles['muted']))
        problems = [[_p(x, styles['head']) for x in ('Aba / portal','Documento','Motivo / horário')]]
        for e in problem_events[:14]:
            problems.append([
                _p(_short(e['tab'] or e['portal_path'], 30), styles['cell']),
                _p(_short(e.get('document_code') or e.get('document_id') or '-', 30), styles['cell']),
                _p(_short(e.get('message') or e['status'], 100) + '\n' +
                   when(e.get('occurred_at')), styles['cell']),
            ])
        story.append(_table(problems, [w*.27,w*.22,w*.51], header=True))

    story.append(PageBreak())
    story.append(Paragraph('04  |  AMOSTRA DE ARQUIVOS E DESTINOS', styles['section']))
    saved = [e for e in final_docs if e['status'] in ('downloaded','skipped')]
    if not saved:
        story.append(_p('Nenhum arquivo registrado como baixado ou já existente.',styles['body']))
    else:
        story.append(_p(f'Exibindo {min(len(saved), 10)} de {len(saved)} arquivos; '
                        'o nome e o caminho completo de TODOS os documentos estão no inventário CSV.',
                        styles['muted']))
        samples = [[_p(x,styles['head']) for x in ('Código / documento','Situação','Destino relativo')]]
        for e in saved[:10]:
            samples.append([
                _p(_short(e['document_code'] or e['document_id'], 30),styles['cell']),
                _p(STATUS.get(e['status'],e['status']),styles['cell']),
                _p(_short(e['destination_path'], 115),styles['cell']),
            ])
        story.append(_table(samples,[w*.24,w*.18,w*.58],header=True))

    story.append(Paragraph('05  |  INVENTÁRIO COMPLETO E RASTREABILIDADE',styles['section']))
    csv_docs = documents_csv_name or path.with_name(path.stem+'-documentos.csv').name
    csv_nav = navigation_csv_name or path.with_name(path.stem+'-navegacao.csv').name
    story.append(_p('Para preservar a legibilidade com milhares de anexos, o PDF é gerencial. '
                    'Os dois arquivos CSV acompanham o relatório na mesma pasta, com todos '
                    'os documentos e toda a navegação, sem corte de registros.',styles['body']))
    paths = [
        ('Documentos (CSV)',csv_docs),
        ('Navegação e alertas (CSV)',csv_nav),
        ('Pasta de armazenamento',str(storage_root)),
        ('Portal',protected_url(portal_url) if portal_url else 'Não informado'),
    ]
    for label,value in paths:
        story.append(Paragraph(f'<b>{escape(label)}</b>',styles['body']))
        story.append(_p(_short(value,160),styles['muted']))
    story.append(Spacer(1,7))
    story.append(_p('Campos do inventário de documentos: data/hora, tentativa, aba, subportal, '
                    'identificação, data do documento, unidade, status, arquivo, destino relativo '
                    'e absoluto, tamanho, SHA-256, URL de origem protegida e motivo de erro.',
                    styles['muted']))
    if job.error_summary:
        story.append(Paragraph('Nota da execução',styles['section']))
        story.append(_p(_short(job.error_summary, 400),styles['body']))
    story.append(Spacer(1,8))
    story.append(_p('Relatório automático baseado nos eventos efetivamente observados. '
                    'Não comprova cobertura total do portal sem conciliação de contagens com a origem. '
                    'Arquivos CSV podem ser filtrados por status, aba, data e documento.',styles['muted']))

    def footer(canvas, page_doc):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(doc.leftMargin, 1.07*cm, pagew-doc.rightMargin, 1.07*cm)
        canvas.setFont('Helvetica',7)
        canvas.setFillColor(MUTED)
        canvas.drawString(doc.leftMargin,.72*cm,'FALCÃO BAUER  |  Auditoria de extração  |  Brasil')
        canvas.drawRightString(pagew-doc.rightMargin,.72*cm,f'Página {page_doc.page}')
        canvas.restoreState()

    doc.build(story,onFirstPage=footer,onLaterPages=footer)


def _hash_file(path: Path) -> tuple[str,int]:
    checksum=hashlib.sha256()
    size=0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024),b''):
            size+=len(chunk)
            checksum.update(chunk)
    return checksum.hexdigest(),size


async def publish_job_pdf(*, job: SyncJob, repository, storage,
                          settings: Settings) -> str | None:
    """Publica inventarios CSV e por ultimo PDF; grava caminho PDF no job."""
    if job.options.get('dry_run', False):
        return None
    try:
        events = await repository.list_audit_events(job.id)
        now = datetime.now(TZ)
        stem = f'Extracao-{now:%Y%m%d-%H%M%S}-{job.id}'
        base = f'Relatorios-de-Execucao/{now:%Y/%m}'
        pdf_name = f'Relatorio-{stem}.pdf'
        docs_name = f'Documentos-{stem}.csv'
        nav_name = f'Navegacao-{stem}.csv'
        # TEMP_DIR e gravavel pelo usuario da imagem Docker (UID 10001).
        settings.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='audit-',dir=settings.TEMP_DIR) as folder:
            temp=Path(folder)
            pdf=temp/pdf_name
            documents=temp/docs_name
            navigation=temp/nav_name
            await asyncio.to_thread(
                write_audit_csvs,documents,navigation,job=job,events=events,
                storage_root=settings.NETWORK_SHARE_ROOT)
            await asyncio.to_thread(
                build_pdf,pdf,job=job,events=events,
                storage_root=settings.NETWORK_SHARE_ROOT,
                portal_url=settings.PORTAL_ENTRY_URL,
                documents_csv_name=docs_name,navigation_csv_name=nav_name)
            # CSVs primeiro; PDF por ultimo indica pacote concluido.
            for file in (documents,navigation,pdf):
                digest,size=await asyncio.to_thread(_hash_file,file)
                await storage.store(file,f'{base}/{file.name}',digest,size)
        report_path=f'{base}/{pdf_name}'
        await repository.record_report_result(job.id,path=report_path)
        return report_path
    except Exception as exc:
        await repository.record_report_result(job.id,error=f'{type(exc).__name__}: {exc}')
        raise


def _demo(output: Path, *, count: int = 30) -> None:
    """Teste com dados ficticios; nao usa credenciais, portal, RDS ou rede SMB."""
    from softexpert_exporter.domain.enums import JobStatus
    now=datetime.now(UTC)
    job=SyncJob(
        id='DEMO-SEM-DADOS-REAIS',status=JobStatus.PARTIAL,
        requested_by='Equipe de homologação',mode='test',options={'max_reports':count},
        created_at=now,started_at=now,finished_at=now,attempts=1,
        total_records=count,downloaded=max(0,count-2),skipped=1,failed=1,
        error_summary='Demonstração fictícia, sem arquivos reais.')
    tabs=['Obras','CQM','FV - Geral','RDF - Civil','Inspeção','FIE','FNC','IQ - Lote 2']
    events=[]
    for tab in tabs:
        events.append(dict(kind='navigation',attempt=1,status='visited',tab=tab,
            portal_path=f'Checklist - Metrô / {tab}',document_id='',document_code='',
            document_date='',unit='',source_url='',file_name='',destination_path='',
            size_bytes=None,checksum_sha256='',message='Acesso efetuado',occurred_at=now))
    for n in range(count):
        tab=tabs[n%len(tabs)]
        status='failed' if n == count-1 else 'skipped' if n == count-2 else 'downloaded'
        code=f'FB-{n+1:05d}'
        relative=f'Checklist - Metrô/{tab}/Unidade A/{code}.pdf'
        events.append(dict(kind='document',attempt=1,status=status,tab=tab,
            portal_path=f'Checklist - Metrô / {tab}',document_id=str(200000+n),
            document_code=code,document_date='29/10/2026',unit='Unidade A',
            source_url=f'https://exemplo.invalid/relatorio?id={n}&token=ocultar',
            file_name=f'{code}.pdf', destination_path=relative,
            size_bytes=23000+n*71 if status=='downloaded' else None,
            checksum_sha256='a'*64 if status=='downloaded' else '',
            message='HTTP 503 de demonstração' if status=='failed' else '',
            occurred_at=now))
    docs=output.with_name(output.stem+'-documentos.csv')
    nav=output.with_name(output.stem+'-navegacao.csv')
    write_audit_csvs(docs,nav,job=job,events=events,storage_root=Path('/mnt/client-share'))
    build_pdf(output,job=job,events=events,storage_root=Path('/mnt/client-share'),
              portal_url='https://exemplo.invalid/soft',demo=True,
              documents_csv_name=docs.name,navigation_csv_name=nav.name)
    print(f'Arquivos de demonstração: {output}, {docs.name}, {nav.name}')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='Relatório compacto de extração')
    parser.add_argument('--demo',action='store_true',help='Usa somente dados fictícios')
    parser.add_argument('--count',type=int,default=30,
                        help='Quantidade de registros fictícios para testar escala')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not args.demo:
        parser.error('CLI disponível somente para --demo; jobs reais são gerados pelo worker.')
    _demo(args.output,count=max(2,args.count))
