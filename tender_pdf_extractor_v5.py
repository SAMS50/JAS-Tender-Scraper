"""JAS Tender metadata extraction V5: conservative text-only recovery.

Run in the existing Tender_Scraper project directory. Does not overwrite V4 outputs.
"""
import csv
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile

import pymupdf

ROOT = Path(__file__).resolve().parent
ZIP_DIR = ROOT / 'downloaded_tenders'
OUTPUT = ROOT / 'tender_metadata_batch_v5.csv'
AUDIT = ROOT / 'tender_metadata_v5_audit.json'
BATCH = [
    'work_341915.zip', 'work_343127.zip', 'work_342456.zip',
    'work_342679.zip', 'work_342996.zip', 'work_343064.zip',
    'work_343288.zip', 'work_343440.zip', 'work_343494.zip',
    'work_343514.zip', 'work_343524.zip', 'work_343534.zip',
    'work_343545.zip', 'work_343550.zip', 'work_343552.zip',
    'work_343553.zip', 'work_343554.zip', 'work_344022.zip',
    'work_344025.zip', 'work_344089.zip',
]
MAX_PAGES = 12
FIELDS = [
    'zip_filename', 'portal_tnid', 'tender_id', 'tender_id_source',
    'tender_id_status', 'organization', 'tender_title',
    'estimated_value_inr', 'estimated_value_basis', 'emd_inr',
    'tender_fee_inr', 'contract_duration', 'document_tender_reference',
    'eligibility_text', 'evidence', 'financial_candidates',
    'metadata_fields_found', 'metadata_fields_total', 'extraction_warnings',
    'conditional_emd_json', 'title_source', 'metadata_review_flags',
]
ORGS = {
    'NTPC-SAIL Power Company Limited': ['NTPC-SAIL POWER COMPANY'],
    'Airports Authority of India': ['AIRPORTS AUTHORITY OF INDIA'],
    'Reserve Bank of India': ['RESERVE BANK OF INDIA'],
    'GAIL (India) Limited': ['GAIL (INDIA) LIMITED'],
    'Oil and Natural Gas Corporation Limited': ['OIL AND NATURAL GAS CORPORATION'],
    'Damodar Valley Corporation': ['DAMODAR VALLEY CORPORATION'],
    'Municipal Corporation of Delhi': ['MUNICIPAL CORPORATION OF DELHI'],
    'Delhi Metro Rail Corporation': ['DELHI METRO RAIL CORPORATION'],
    'Bharat Sanchar Nigam Limited': ['BHARAT SANCHAR NIGAM LIMITED', 'BSNL ODISHA CIRCLE'],
}
ID_RE = re.compile(r'\b20\d{2}_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_\d+_\d+\b')
AMOUNT_RE = re.compile(
    r'(?:₹|Rs\.?|INR)\s*(?P<num>\d[\d,]*(?:\.\d+)?)'
    r'\s*(?P<unit>lakhs?|lacs?|crores?|cr\.?)?', re.I
)
LABELS = {
    'estimated_value_inr': [r'estimated\s+cost(?:\s+of\s+work)?', r'estimated\s+value', r'estimated\s+amount'],
    'emd_inr': [r'earnest\s+money(?:\s+deposit)?', r'\bEMD\s+amount\b', r'\bEMD\s*(?:of|:|-)'],
    'tender_fee_inr': [r'tender\s+(?:processing\s+)?fee', r'cost\s+of\s+tender\s+document'],
}
TITLE_LABELS = [
    r'name\s+of\s+(?:the\s+)?work(?:s)?',
    r'brief\s+description\s+of\s+work',
    r'name\s+of\s+package',
    r'scope\s+of\s+(?:supply|procurement)',
]
TITLE_STOP = re.compile(
    r'^(?:estimated\s+cost|tender\s+(?:id|no|fee)|emd|earnest\s+money|'
    r'completion\s+period|date\s+of|notice\s+inviting|invitation\s+for|'
    r'eligibility|qualifying\s+requirement|important\s+dates|'
    r'section\s+[ivx\d]+|page\s+\d+|signature|'
    r'for\s+and\s+on\s+behalf|payment\s+terms|performance\s+bank)', re.I
)
DURATION_RE = re.compile(r'\b\d+\s*(?:\([^)]*\)\s*)?(?:days?|months?|weeks?|years?)\b', re.I)


def norm(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def load_mappings():
    result = {}
    for filename in ('tender_zip_mapping.csv', 'tender_document_mapping.csv'):
        path = ROOT / filename
        if not path.exists():
            print('Mapping not found:', filename)
            continue
        with path.open(encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                name = norm(row.get('zip_filename'))
                if not name:
                    continue
                entry = result.setdefault(name, {'ids': set(), 'tnids': set()})
                if norm(row.get('tender_id')):
                    entry['ids'].add(norm(row['tender_id']))
                if norm(row.get('portal_tnid')):
                    entry['tnids'].add(norm(row['portal_tnid']))
    return result


def amount_value(match):
    value = Decimal(match.group('num').replace(',', ''))
    unit = (match.group('unit') or '').lower()
    if unit.startswith(('lakh', 'lac')):
        value *= 100000
    elif unit.startswith(('crore', 'cr')):
        value *= 10000000
    return value.quantize(Decimal('0.01'))


def gst_basis(text):
    if re.search(r'(?:excluding|exclusive\s+of|without)\s+gst', text, re.I):
        return 'excluding_gst'
    if re.search(r'(?:including|inclusive\s+of|with)\s+gst', text, re.I):
        return 'including_gst'
    return 'unspecified'


def period_basis(text):
    if re.search(r'\b(?:two|2)\s+years?\b', text, re.I):
        return 'two_years'
    if re.search(r'\b(?:one|1)\s+years?\b', text, re.I):
        return 'one_year'
    return 'unspecified'


def classify_context(text, field):
    if field == 'estimated_value_inr' and re.search(
        r'30\s*%\s+of\s+(?:the\s+)?estimated|turnover|financial\s+information|'
        r'similar\s+work|minimum\s+annual|experience\s+criteria', text, re.I
    ):
        return 'eligibility_reference'
    return 'tender_financial_candidate'


def extract_money(lines, source):
    candidates = []
    for i, line in enumerate(lines):
        for field, patterns in LABELS.items():
            label = next((m for p in patterns if (m := re.search(p, line, re.I))), None)
            if not label:
                continue
            after_label = line[label.end():]
            match = AMOUNT_RE.search(after_label)
            amount_line = after_label
            if not match:
                for offset in range(1, 5):
                    if i + offset >= len(lines):
                        break
                    next_line = lines[i + offset]
                    if any(re.search(p, next_line, re.I) for pats in LABELS.values() for p in pats):
                        break
                    match = AMOUNT_RE.search(next_line)
                    if match:
                        amount_line = next_line
                        break
            if not match:
                continue
            context = norm(' '.join(lines[max(0, i-1):min(len(lines), i+6)]))
            local_basis = amount_line[max(0, match.start()-35):match.end()+130]
            classification = classify_context(context, field)
            if field == 'estimated_value_inr' and re.search(
                r'\b(?:at\s+least|30\s*%|turnover|financial\s+year)', context, re.I
            ):
                classification = 'eligibility_reference'
            candidates.append({
                'field': field, 'value_inr': str(amount_value(match)),
                'gst_basis': gst_basis(local_basis), 'period': period_basis(local_basis),
                'classification': classification, 'document': source[0],
                'page': source[1], 'context': context[:550],
                'method': 'v4_label',
            })
            break
    return candidates


def clean_title(text):
    text = norm(text)
    text = re.sub(r'^[\s:–—/-]+', '', text)
    text = re.sub(r'^(?:PROCUREMENT\s+)+(?=PROCUREMENT\s+OF)', '', text, flags=re.I)
    text = re.sub(r'\s+(?:estimated\s+cost|tender\s+id|emd\s+amount)\s*[:\-].*$', '', text, flags=re.I)
    return norm(text)


def title_looks_valid(text):
    return 25 <= len(text) <= 350 and not re.match(
        r'^(?:goods\s*:|a\s+sum\s+equivalent|payment\s+shall|the\s+contractor)', text, re.I
    )


def collect_title(lines, start, initial):
    pieces = [initial] if initial else []
    for line in lines[start+1:start+8]:
        if TITLE_STOP.search(line) or not line or len(norm(' '.join(pieces))) > 300:
            break
        if pieces and pieces[-1].rstrip().endswith('.'):
            break
        pieces.append(line)
    return clean_title(' '.join(pieces))


def extract_title(lines):
    for i, line in enumerate(lines[:100]):
        for label in TITLE_LABELS:
            match = re.search(label + r'\s*[:\-]?\s*', line, re.I)
            if match:
                title = collect_title(lines, i, line[match.end():])
                if title_looks_valid(title):
                    return title
    for i, line in enumerate(lines[:35]):
        match = re.search(r'(?:TENDER\s+DOCUMENT\s+FOR|FOR\s+PROCUREMENT\s+OF|E-TENDER\s+FOR)\s+(.+)', line, re.I)
        if match:
            title = collect_title(lines, i, match.group(1))
            if title_looks_valid(title):
                return title
    return ''


def extract_organization(text):
    upper = norm(text[:3500]).upper()
    for name, aliases in ORGS.items():
        if any(alias in upper for alias in aliases):
            return name
    return ''


def extract_duration(lines):
    for i, line in enumerate(lines):
        for label in (r'completion\s+period', r'period\s+of\s+contract', r'time\s+allowed', r'contract\s+duration'):
            match = re.search(label, line, re.I)
            if match:
                segment = ' '.join([line[match.end():]] + lines[i+1:i+2])
                duration = DURATION_RE.search(segment)
                if duration:
                    return norm(duration.group())
    for line in lines:
        match = re.search(r'(?:completed|completion)\s+within\s+(\d+\s+(?:days?|months?|years?))', line, re.I)
        if match:
            return norm(match.group(1))
    return ''


def resolve_financial(field, candidates):
    relevant = [c for c in candidates if c['field'] == field and c['classification'] == 'tender_financial_candidate']
    if not relevant:
        return '', '', 'No accepted candidate'
    grouped = {}
    for c in relevant:
        grouped.setdefault(c['value_inr'], []).append(c)
    if field == 'estimated_value_inr' and len(grouped) > 1:
        two_year = {v: items for v, items in grouped.items() if any(i['period'] == 'two_years' for i in items)}
        if len(two_year) == 1 and any(c['period'] == 'one_year' for c in relevant):
            grouped = two_year
    if len(grouped) != 1:
        return '', '', 'Conflicting amounts: ' + ', '.join(sorted(grouped))
    value, items = next(iter(grouped.items()))
    explicit_gst = {i['gst_basis'] for i in items if i['gst_basis'] != 'unspecified'}
    explicit_period = {i['period'] for i in items if i['period'] != 'unspecified'}
    if len(explicit_gst) > 1:
        return '', '', 'Conflicting GST basis'
    if len(explicit_period) > 1:
        return '', '', 'Conflicting contract periods'
    return value, f"{next(iter(explicit_gst), 'unspecified')};{next(iter(explicit_period), 'unspecified')}", ''


def conditional_emd(lines, source):
    """Only accept amounts explicitly labelled with GAIL bidding-group conditions."""
    found = []
    for i, line in enumerate(lines):
        for scope, pattern in (
            ('both_groups', r'(?:cumulative\s+EMD\s+for\s+both\s+(?:the\s+)?groups)\s*(?:[:\-]|is)?\s*(?:INR|Rs\.?|₹)\s*([\d,.]+)\s*(lakhs?|lacs?)'),
            ('group_1', r'EMD\s+for\s+Group\s*1\s*:\s*(?:Rs\.?|INR|₹)\s*([\d,.]+)\s*(lakhs?|lacs?)'),
            ('group_2', r'EMD\s+for\s+Group\s*2\s*:\s*(?:Rs\.?|INR|₹)\s*([\d,.]+)\s*(lakhs?|lacs?)'),
        ):
            context = norm(' '.join(lines[i:i+3]))
            match = re.search(pattern, context, re.I)
            if not match:
                continue
            value = Decimal(match.group(1).replace(',', '')) * 100000
            item = {
                'scope': scope, 'currency': 'INR',
                'value': str(value.quantize(Decimal('0.01'))),
                'bidder_condition': 'Indian bidders',
                'document': source[0], 'page': source[1],
                'evidence': context[:400],
            }
            if not any(x['scope'] == scope and x['value'] == item['value'] for x in found):
                found.append(item)
    return found


def targeted_recovery(name, pages, row, candidates, evidence, flags, warnings):
    """Evidence-gated improvements; do not use hardcoded values without source text."""
    if name == 'work_343064.zip' and not row['tender_title']:
        for page in pages:
            text = page['text']
            match = re.search(
                r'(?:i\.\s*)?Name\s+of\s+works\s*[:\-]?\s*'
                r'(Supply,?\s+Installation,?\s+Testing\s+and\s+Commissioning\s*\(SITC\).*?)'
                r'(?=\n\s*(?:ii\.|iii\.|Estimated\s+cost|Payment\s+Terms)|\Z)',
                text, re.I | re.S
            )
            if match:
                title = norm(match.group(1))
                if title_looks_valid(title):
                    row['tender_title'] = title
                    row['title_source'] = f"{page['document']}:page{page['page']}:name_of_works"
                    evidence.append('tender_title=' + row['title_source'])
                    break

    if name == 'work_343545.zip':
        for page in pages:
            if not re.search(r'ConditionsTyre\.pdf$', page['document'], re.I):
                continue
            text = page['text']
            title_match = re.search(r'Name\s+of\s+Work\s*:\s*(.+)', text, re.I)
            if title_match and not row['tender_title']:
                row['tender_title'] = clean_title(title_match.group(1))
                row['title_source'] = f"{page['document']}:page{page['page']}:name_of_work"
            for field, pattern in (
                ('estimated_value_inr', r'Estimated\s+cost\s+of\s+work\s*:\s*'),
                ('emd_inr', r'(?:i\)\s*)?Earnest\s+Money\s*:\s*'),
            ):
                match = re.search(pattern, text, re.I)
                if not match:
                    continue
                value_match = AMOUNT_RE.search(text[match.end():match.end()+85])
                if not value_match:
                    continue
                value = str(amount_value(value_match))
                candidates.append({
                    'field': field, 'value_inr': value, 'gst_basis': 'unspecified',
                    'period': 'unspecified', 'classification': 'tender_financial_candidate',
                    'document': page['document'], 'page': page['page'],
                    'context': norm(text[max(0, match.start()-50):match.end()+125]),
                    'method': 'v5_explicit_mcd_tender_field',
                })
                evidence.append(f"{field}={page['document']}:page{page['page']}:explicit_label")
        # MCD boilerplate thresholds (e.g. Rs 15 lakh) must not override explicit work-specific amounts.
        explicit = [c for c in candidates if c.get('method') == 'v5_explicit_mcd_tender_field']
        for field in ('estimated_value_inr', 'emd_inr'):
            vals = {c['value_inr'] for c in explicit if c['field'] == field}
            if len(vals) == 1:
                row[field] = next(iter(vals))
                if field == 'estimated_value_inr':
                    row['estimated_value_basis'] = 'unspecified;unspecified'
                flags.append(f'{field}:explicit_document_value_overrides_generic_clause_candidates')
            elif len(vals) > 1:
                warnings.append(f'{field}: conflicting explicit MCD values')

    if name == 'work_343494.zip':
        for page in pages:
            if page['page'] != 1:
                continue
            match = re.search(
                r'Procurement\s+of\s+Molecular\s+Sieves\s+and\s+Support\s+Balls.*?GAIL,\s*Usar\.',
                norm(page['text']), re.I
            )
            if match:
                row['tender_title'] = norm(match.group())
                row['title_source'] = f"{page['document']}:page1:cover_description"
                evidence.append('tender_title=' + row['title_source'])
                break


def process_zip(name, mappings):
    row = {field: '' for field in FIELDS}
    row['zip_filename'] = name
    warnings, candidates, evidence, pdf_ids, pages, flags = [], [], [], set(), [], []
    conditional = []
    blank_pages = readable_pages = 0
    mapping = mappings.get(name, {'ids': set(), 'tnids': set()})
    if len(mapping['tnids']) == 1:
        row['portal_tnid'] = next(iter(mapping['tnids']))
    if len(mapping['ids']) == 1:
        row['tender_id'] = next(iter(mapping['ids']))
        row['tender_id_source'] = 'mapping'
        row['tender_id_status'] = 'mapped'
    elif len(mapping['ids']) > 1:
        row['tender_id_status'] = 'mapping_conflict'
        warnings.append('Conflicting mapped tender IDs')
    path = ZIP_DIR / name
    if not path.exists():
        warnings.append('ZIP not found')
    else:
        try:
            with ZipFile(path) as archive:
                members = sorted(
                    [m for m in archive.namelist() if m.lower().endswith('.pdf')],
                    key=lambda x: (0 if re.search(r'tender|nit|qualif', x, re.I) else 1, x.lower())
                )
                if not members:
                    warnings.append('No PDF documents')
                for member in members:
                    filename = Path(member).name
                    try:
                        with pymupdf.open(stream=archive.read(member), filetype='pdf') as pdf:
                            for page_no in range(min(MAX_PAGES, len(pdf))):
                                text = pdf[page_no].get_text()
                                if not norm(text):
                                    blank_pages += 1
                                    continue
                                readable_pages += 1
                                lines = [norm(s) for s in text.splitlines() if norm(s)]
                                source = (filename, page_no+1)
                                pages.append({'document': filename, 'page': page_no+1, 'text': text})
                                pdf_ids.update(ID_RE.findall(text))
                                if not row['organization']:
                                    row['organization'] = extract_organization(text)
                                if not row['tender_title'] and page_no < 3:
                                    row['tender_title'] = extract_title(lines)
                                    if row['tender_title']:
                                        row['title_source'] = f'{filename}:page{page_no+1}:v4_title'
                                if not row['contract_duration']:
                                    row['contract_duration'] = extract_duration(lines)
                                candidates.extend(extract_money(lines, source))
                                if name == 'work_343494.zip':
                                    conditional.extend(conditional_emd(lines, source))
                                if not row['eligibility_text'] and re.search(r'qualifying\s+requirements|technical\s+criteria', text, re.I):
                                    row['eligibility_text'] = norm(text)[:3500]
                    except Exception as exc:
                        warnings.append(f'{filename}: {exc}')
        except Exception as exc:
            warnings.append(f'ZIP error: {exc}')

    if not row['tender_id'] and row['tender_id_status'] != 'mapping_conflict':
        if len(pdf_ids) == 1:
            row['tender_id'] = next(iter(pdf_ids))
            row['tender_id_source'] = 'pdf_candidate'
            row['tender_id_status'] = 'needs_review'
        else:
            row['tender_id_status'] = 'missing_tender_id'
    if row['tender_id'] and pdf_ids and row['tender_id'] not in pdf_ids:
        warnings.append('Mapped/PDF tender ID disagreement')

    targeted_recovery(name, pages, row, candidates, evidence, flags, warnings)
    for field in LABELS:
        if row[field]:
            continue  # Preserve evidence-gated explicit recovery.
        value, basis, warning = resolve_financial(field, candidates)
        row[field] = value
        if field == 'estimated_value_inr':
            row['estimated_value_basis'] = basis
        if warning and warning != 'No accepted candidate':
            warnings.append(f'{field}: {warning}')
        if value:
            accepted = next((c for c in candidates if c['field'] == field and c['value_inr'] == value and c['classification'] == 'tender_financial_candidate'), None)
            if accepted:
                evidence.append(f"{field}={accepted['document']}:page{accepted['page']}")

    # Multi-group EMD must never be collapsed into a misleading single value.
    if name == 'work_343494.zip':
        unique = {(c['scope'], c['value']) : c for c in conditional}
        conditional = list(unique.values())
        if conditional:
            row['emd_inr'] = ''
            flags.append('emd_inr:conditional_by_bidding_group')
            warnings.append('EMD varies by group; inspect conditional_emd_json')
    if blank_pages:
        warnings.append(f'{blank_pages} pages without extractable text')
    if not readable_pages:
        warnings.append('No readable PDF text')
    if row['tender_id_status'] in ('missing_tender_id', 'mapping_conflict', 'needs_review'):
        flags.append('tender_id:requires_verification')

    row['conditional_emd_json'] = json.dumps(conditional, ensure_ascii=False)
    row['financial_candidates'] = json.dumps(candidates, ensure_ascii=False)
    row['evidence'] = '; '.join(dict.fromkeys(evidence))
    row['metadata_review_flags'] = ' | '.join(dict.fromkeys(flags))
    core = ('organization', 'tender_title', 'estimated_value_inr', 'emd_inr', 'tender_fee_inr', 'contract_duration')
    row['metadata_fields_found'] = sum(bool(row[f]) for f in core)
    row['metadata_fields_total'] = 6
    row['extraction_warnings'] = ' | '.join(dict.fromkeys(warnings))
    return row


def main():
    mappings = load_mappings()
    rows = []
    print('='*65)
    print('JAS METADATA EXTRACTION - VALIDATION V5')
    print('='*65)
    for index, name in enumerate(BATCH, 1):
        row = process_zip(name, mappings)
        rows.append(row)
        print(f"[{index:02d}/{len(BATCH)}] {name} | ID: {row['tender_id'] or 'MISSING'} | "
              f"Title: {'YES' if row['tender_title'] else 'NO'} | "
              f"Estimate: {row['estimated_value_inr'] or 'MISSING'} | "
              f"EMD: {row['emd_inr'] or ('CONDITIONAL' if json.loads(row['conditional_emd_json']) else 'MISSING')}")
    with OUTPUT.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    coverage = {field: sum(bool(row[field]) for row in rows) for field in
                ('tender_id', 'organization', 'tender_title', 'estimated_value_inr', 'emd_inr', 'contract_duration')}
    audit = {
        'version': 'v5', 'tenders': len(rows), 'field_coverage': coverage,
        'id_statuses': dict(Counter(r['tender_id_status'] for r in rows)),
        'records': [{
            'zip_filename': r['zip_filename'], 'tender_id': r['tender_id'],
            'tender_title': r['tender_title'], 'title_source': r['title_source'],
            'estimated_value_inr': r['estimated_value_inr'],
            'estimated_value_basis': r['estimated_value_basis'],
            'emd_inr': r['emd_inr'],
            'conditional_emd': json.loads(r['conditional_emd_json']),
            'review_flags': r['metadata_review_flags'],
            'warnings': r['extraction_warnings'],
            'financial_candidates': json.loads(r['financial_candidates']),
        } for r in rows],
    }
    AUDIT.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding='utf-8')
    print('\nV5 COVERAGE')
    for field, count in coverage.items():
        print(f'  {field}: {count}/{len(rows)}')
    print('Saved:', OUTPUT.name)
    print('Saved:', AUDIT.name)
    print('V4 files unchanged.')


if __name__ == '__main__':
    main()
