from datetime import date, datetime, timezone
from softexpert_exporter.date_windows import parse_report_date, previous_six_months, scheduled_semester, eligible_report

def test_parse_pt_br():
    assert parse_report_date('Emissao 14/10/2026') == date(2026, 10, 14)
    assert parse_report_date('2026-10-14') == date(2026, 10, 14)
    assert parse_report_date('data desconhecida') is None
    assert parse_report_date('31/02/2026') is None

def test_months():
    assert previous_six_months(date(2027, 1, 1)) == date(2026, 7, 1)
    assert previous_six_months(date(2026, 7, 1)) == date(2026, 1, 1)
    assert previous_six_months(date(2026, 8, 31)) == date(2026, 2, 28)

def test_scheduled_semesters():
    assert scheduled_semester(datetime(2027, 1, 1, 5, 0, tzinfo=timezone.utc), 'America/Sao_Paulo') == (date(2026, 7, 1), date(2027, 1, 1))
    assert scheduled_semester(datetime(2027, 7, 1, 5, 0, tzinfo=timezone.utc), 'America/Sao_Paulo') == (date(2027, 1, 1), date(2027, 7, 1))

def test_date_filter():
    hist = {'cutoff_date':'2026-10-22'}
    assert eligible_report('historical', '22/10/2026', hist)[0]
    assert not eligible_report('historical', '23/10/2026', hist)[0]
    inc = {'window_start':'2026-07-01', 'window_end':'2027-01-01'}
    assert eligible_report('incremental', '01/07/2026', inc)[0]
    assert eligible_report('incremental', '31/12/2026', inc)[0]
    assert not eligible_report('incremental', '01/01/2027', inc)[0]
    assert eligible_report('incremental', '', inc) == (False, 'missing_or_invalid_date')
