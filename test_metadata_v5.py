"""V5 regression checks. Run after tender_pdf_extractor_v5.py."""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
path = ROOT / 'tender_metadata_batch_v5.csv'
if not path.exists():
    raise SystemExit('Run python tender_pdf_extractor_v5.py first')
with path.open(encoding='utf-8-sig', newline='') as f:
    rows = {r['zip_filename']: r for r in csv.DictReader(f)}

checks = [
    ('NTPC estimate', 'work_341915.zip', 'estimated_value_inr', '10502062.35'),
    ('AAI Kolkata estimate', 'work_343127.zip', 'estimated_value_inr', '54702214.00'),
    ('AAI Kolkata EMD', 'work_343127.zip', 'emd_inr', '1641066.00'),
    ('AAI Gondia estimate', 'work_342456.zip', 'estimated_value_inr', '4471306.00'),
    ('AAI Gondia EMD', 'work_342456.zip', 'emd_inr', '89426.00'),
    ('RBI estimate', 'work_343064.zip', 'estimated_value_inr', '6108000.00'),
    ('RBI basis', 'work_343064.zip', 'estimated_value_basis', 'including_gst;unspecified'),
    ('RBI EMD', 'work_343064.zip', 'emd_inr', '122160.00'),
    ('ONGC EMD', 'work_343514.zip', 'emd_inr', '228000.00'),
    ('DVC two-year estimate', 'work_343524.zip', 'estimated_value_inr', '2828575.20'),
    ('DVC basis', 'work_343524.zip', 'estimated_value_basis', 'excluding_gst;two_years'),
    ('MCD estimate', 'work_343545.zip', 'estimated_value_inr', '477621.00'),
    ('MCD EMD', 'work_343545.zip', 'emd_inr', '9098.00'),
]
passed = 0
for label, name, field, expected in checks:
    actual = rows.get(name, {}).get(field, '')
    ok = actual == expected
    passed += ok
    print(f"{'PASS' if ok else 'FAIL'} | {label} | expected={expected} actual={actual or 'MISSING'}")
for label, name, text in [
    ('NTPC title', 'work_341915.zip', 'NSPCL Rourkela'),
    ('AAI Kolkata title', 'work_343127.zip', 'Kolkata'),
    ('AAI Gondia title', 'work_342456.zip', 'Gondia'),
    ('GAIL title', 'work_343494.zip', 'GAIL, Usar'),
    ('ONGC title', 'work_343514.zip', 'Cauvery Asset'),
    ('RBI title', 'work_343064.zip', '400 kVA'),
]:
    actual = rows.get(name, {}).get('tender_title', '')
    ok = text.lower() in actual.lower()
    passed += ok
    print(f"{'PASS' if ok else 'FAIL'} | {label} | {actual or 'MISSING'}")
conditional = json.loads(rows['work_343494.zip']['conditional_emd_json'])
amounts = {c['scope']: c['value'] for c in conditional}
for scope, expected in [('both_groups', '2000000.00'), ('group_1', '1800000.00'), ('group_2', '200000.00')]:
    actual = amounts.get(scope)
    ok = actual == expected
    passed += ok
    print(f"{'PASS' if ok else 'FAIL'} | GAIL {scope} | {actual}")
ok = rows['work_343494.zip']['emd_inr'] == ''
passed += ok
print(f"{'PASS' if ok else 'FAIL'} | GAIL scalar EMD remains empty")
for name in ('work_342996.zip', 'work_343440.zip'):
    ok = rows[name]['tender_id'] == ''
    passed += ok
    print(f"{'PASS' if ok else 'FAIL'} | unresolved ID remains blank: {name}")
total = len(checks) + 6 + 3 + 1 + 2
print(f'RESULT: {passed}/{total} PASSED')
if passed != total:
    raise SystemExit(1)
