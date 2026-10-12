"""Conservative normalization: keep all source cells; never guess unknown limits."""
import re
from collections import Counter
from decimal import Decimal
from bs4 import BeautifulSoup

# Bump when parsing or flagging rules change. Stored per norm version; a parser change alone
# never creates a new norm version (versions are compared on the extracted raw cells).
PARSER_VERSION = '1'
ISO_NORM = 'DIN EN ISO 19598'
ISO_CODE = re.compile(r'^(Fe)/{1,2}(ZnNi|ZnFe|Zn)(\d+)/{1,2}([A-Z])(n?)/{1,2}(T\d)$')
LETTERS = {'A': 'transparent', 'C': 'thick_layer', 'F': 'black'}

def normalise_type_code(raw):
    stripped = raw.strip()
    compact = re.sub(r'\s+', '', stripped)
    match = ISO_CODE.fullmatch(compact)
    flags = ['whitespace_trimmed'] if stripped != raw else []
    if not match:
        return stripped, flags
    sub, system, thickness, pas, n, seal = match.groups()
    fixed = f'{sub}//{system}{thickness}//{pas}{n}//{seal}'
    if compact != stripped:
        flags.append('whitespace_removed')
    if fixed != compact:
        flags.append('slash_fixed')
    return fixed, flags

def parse_thickness(raw):
    value = re.sub(r'\s*(?:µm|μm|um)\s*$', '', raw.strip(), flags=re.I)
    m = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*[-–—]\s*(\d+(?:[.,]\d+)?)', value)
    if m:
        low, high = [Decimal(v.replace(',', '.')) for v in m.groups()]
        if low > high:
            raise ValueError('Reversed thickness interval')
        return low, high
    if re.fullmatch(r'\d+(?:[.,]\d+)?', value):
        return Decimal(value.replace(',', '.')), None
    if not value or value in ('-', '–'):
        return None, None
    raise ValueError('Unrecognized thickness notation')

def yes_no(value):
    return {'ja': True, 'yes': True, 'x': True, 'nein': False, 'no': False, '-': False}.get(value.strip().lower())

def attributes_from_description(desc):
    d = desc.lower()
    system = ('ZnNi' if 'zink-nickel' in d else 'ZnFe' if 'zink-eisen' in d else
              'Zn' if 'zink' in d and 'phosphat' not in d and 'lamellen' not in d else None)
    pas = ('black' if 'schwarz' in d else 'thick_layer' if 'dickschicht' in d else
           'transparent' if 'transparent' in d else 'iridescent' if 'irisierend' in d else None)
    seal = None if 'wahlfrei' in d else False if 'ohne versiegelung' in d else True if 'versiegelung' in d else None
    return dict(coating_system=system, passivation=pas, sealed=seal)

def norm_key(name):
    """Norm names compare with collapsed whitespace (one published title has a double space)."""
    return re.sub(r'\s+', ' ', name.strip())

def iso_single_value_as_minimum(row, norm_code):
    return norm_key(norm_code) == ISO_NORM and row['thickness_min_um'] is not None and row['thickness_max_um'] is None

def clean_rows(rows, norm_code=ISO_NORM):
    iso = norm_key(norm_code) == ISO_NORM
    counts = Counter(normalise_type_code(r['typ'])[0] for r in rows)
    result = []
    for number, raw in enumerate(rows, 1):
        code, flags = normalise_type_code(raw['typ'])
        attrs = attributes_from_description(raw['beschichtung'])
        try:
            low, high = parse_thickness(raw['schichtdicke'])
        except ValueError:
            low, high = None, None
            flags.append('thickness_unparsed')
        if low is None:
            flags.append('thickness_missing')
        elif high is None and not iso:
            low = None
            flags.append('single_thickness_semantics_unconfirmed')
        # ISO 19598 single values stay as minimum local thickness. That is an interpretation, not a
        # source ambiguity: spec.release_check prints it on every affected report and the loader counts it.
        if counts[code] > 1:
            flags.append('type_code_not_unique')
        if not code:
            flags.append('type_code_missing')
        if attrs['coating_system'] is None:
            flags.append('coating_system_unparsed')
        match = ISO_CODE.fullmatch(code)
        if match:
            _, system, thickness, pas, _, seal = match.groups()
            if system != attrs['coating_system']:
                flags.append('code_vs_description_system')
            if LETTERS.get(pas) != attrs['passivation']:
                flags.append('code_vs_description_passivation')
            if {'T0': False, 'T2': True}.get(seal) != attrs['sealed']:
                flags.append('code_vs_description_sealing')
            if Decimal(thickness) != low:
                flags.append('code_vs_table_thickness')
        for key in ('trommel', 'gestell'):
            if yes_no(raw[key]) is None:
                flags.append(key + '_unparsed')
        if any('\ufffd' in value for value in raw.values()):
            flags.append('source_replacement_character')
        result.append(dict(row_no=number, type_code_raw=raw['typ'], type_code=code,
                           description_de=raw['beschichtung'], thickness_min_um=low,
                           thickness_max_um=high, barrel_ok=yes_no(raw['trommel']),
                           rack_ok=yes_no(raw['gestell']), raw_cells=raw, quality_flags=flags, **attrs))
    return result

def parse_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.find('h1')
    meta = soup.find('meta', attrs={'property': 'article:modified_time'})
    canonical = soup.find('link', rel='canonical')
    rows = []
    expected = ['typ', 'beschichtung', 'schichtdicke', 'trommel', 'gestell']
    tables = 0
    for table in soup.find_all('table'):
        trs = table.find_all('tr')
        if not trs:
            continue
        headings = [c.get_text(' ', strip=True).lower() for c in trs[0].find_all(['th', 'td'])]
        headings = ['schichtdicke' if h.startswith('schichtdicke') else h for h in headings]
        if not all(key in headings for key in expected):
            continue
        tables += 1
        for tr in trs[1:]:
            cells = [c.get_text(' ', strip=True) for c in tr.find_all(['th', 'td'])]
            if not any(cells):
                continue
            if len(cells) != len(headings) or any(c.get('rowspan') or c.get('colspan') for c in tr.find_all(['th','td'])):
                raise ValueError('Unsupported merged or ragged table row')
            rows.append({key: cells[headings.index(key)] for key in expected})
    if not tables or not rows or title is None:
        raise ValueError(f'Expected supported nonempty tables, found {tables}')
    return dict(norm_code=title.get_text(' ', strip=True), modified=meta.get('content') if meta else None,
                url=canonical.get('href') if canonical else None, rows=rows)
