"""Sequential, cached acquisition with robots checks and per-page evidence.

Default: use the local cache and download only pages that are missing from it.
--offline: never touch the network; verify the cache against its manifest and fail if incomplete.
--refresh-norms: re-download robots.txt, the index and every norm page (archiving replaced HTML).

Set NORM_FETCH_USER_AGENT to a User-Agent that includes your contact address before downloading.
"""
import hashlib
import argparse
import json
import os
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / 'data/raw/partec_norms'
BASE = 'https://www.partec.de'
INDEX = BASE + '/industrie-automobilnormen/'
DEFAULT_UA = 'coating-spec-catalog/1.1 (educational portfolio project; set NORM_FETCH_USER_AGENT with a contact address)'
SUPPLIER_PDF = 'https://nmfrc.org/pdf/sf2008/sf0828.pdf'


def index_links(html):
    links = set()
    for anchor in BeautifulSoup(html, 'html.parser').find_all('a', href=True):
        url = urljoin(INDEX, anchor['href']).split('#')[0].split('?')[0].rstrip('/') + '/'
        parsed = urlsplit(url)
        if parsed.netloc == 'www.partec.de' and parsed.path.startswith('/industrie-automobilnormen/') and len(parsed.path.strip('/').split('/')) == 2:
            links.add(url)
    return links


def cached_ok(manifest, url, raw=None):
    raw = raw or RAW
    entry = manifest.get(url, {})
    path = raw / entry.get('file', '')
    return (entry.get('status') == 'ok' and path.is_file()
            and hashlib.sha256(path.read_bytes()).hexdigest() == entry.get('sha256'))


def cache_problems(raw=None):
    """Return (links, problems) for the local cache without any network access."""
    raw = raw or RAW
    manifest_path = raw / 'manifest.json'
    if not manifest_path.exists():
        return set(), ['no manifest.json']
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not cached_ok(manifest, INDEX, raw):
        return set(), ['index page missing or changed']
    links = index_links((raw / manifest[INDEX]['file']).read_text(encoding='utf-8'))
    problems = [url for url in sorted(links) if not cached_ok(manifest, url, raw)]
    if not links:
        problems.append('index yielded no norm links')
    return links, problems


def write_summary(manifest, links):
    """docs/results/acquisition.json: a local build output (git-ignored), never the loader's input."""
    summary = {'discovered': len(links), 'pages': {u: manifest[u] for u in sorted(links)}}
    out = ROOT / 'docs/results'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'acquisition.json').write_bytes(json.dumps(summary, indent=2).encode('utf-8'))


def offline():
    links, problems = cache_problems()
    if problems:
        print('Norm cache incomplete: ' + ', '.join(problems[:5]) + (' ...' if len(problems) > 5 else ''), file=sys.stderr)
        raise SystemExit(1)
    manifest = json.loads((RAW / 'manifest.json').read_text(encoding='utf-8'))
    write_summary(manifest, links)
    print(f'Offline: {len(links)} cached norm pages verified against data/raw/partec_norms/manifest.json')


def download(refresh):
    import requests
    ua = os.environ.get('NORM_FETCH_USER_AGENT') or DEFAULT_UA
    if 'NORM_FETCH_USER_AGENT' not in os.environ:
        print('Warning: NORM_FETCH_USER_AGENT is not set; the default User-Agent has no contact address.', file=sys.stderr)
    RAW.mkdir(parents=True, exist_ok=True)
    manifest_path = RAW / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    session = requests.Session()
    session.headers['User-Agent'] = ua
    robots_path = RAW / 'robots.txt'
    if refresh or not robots_path.exists():
        robots = session.get(BASE + '/robots.txt', timeout=45)
        robots.raise_for_status()
        robots_path.write_text(robots.text, encoding='utf-8')
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(robots_path.read_text(encoding='utf-8').splitlines())
    delay = max(3, rp.crawl_delay(ua) or 0)
    last_request = time.monotonic()

    def save_manifest():
        manifest_path.write_bytes(json.dumps(manifest, indent=2).encode('utf-8'))

    def fetch(url, name, kind):
        nonlocal last_request
        if not rp.can_fetch(ua, url):
            raise RuntimeError('Disallowed by robots.txt')
        path = RAW / name
        if not refresh and cached_ok(manifest, url):
            manifest[url].setdefault('kind', kind)
            return path.read_text(encoding='utf-8')
        time.sleep(max(0, delay - (time.monotonic() - last_request)))
        response = session.get(url, timeout=45)
        last_request = time.monotonic()
        response.raise_for_status()
        response.encoding = 'utf-8'
        if path.exists():
            archive = RAW / 'history'
            archive.mkdir(exist_ok=True)
            (archive / (hashlib.sha256(path.read_bytes()).hexdigest() + '.html')).write_bytes(path.read_bytes())
        path.write_text(response.text, encoding='utf-8')
        manifest[url] = dict(status='ok', kind=kind, file=name, fetched_at=datetime.now(timezone.utc).isoformat(),
                             sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                             last_modified=response.headers.get('Last-Modified'))
        save_manifest()
        return response.text

    links = index_links(fetch(INDEX, '_index.html', 'index'))
    if not links:
        raise RuntimeError('Index yielded no norm links')
    for i, url in enumerate(sorted(links), 1):
        try:
            fetch(url, url.rstrip('/').split('/')[-1] + '.html', 'norm')
            manifest[url].pop('listed', None)
            print(f'{i}/{len(links)} {url}', flush=True)
        except Exception as exc:
            # A failed refresh must not discard a good cached copy: keep the entry and note the error.
            if cached_ok(manifest, url):
                manifest[url]['last_error'] = str(exc)
                manifest[url]['last_error_at'] = datetime.now(timezone.utc).isoformat()
                print(f'{i}/{len(links)} {url}: download failed, keeping cached copy ({exc})', file=sys.stderr)
            else:
                manifest[url] = dict(status='failed', kind='norm', reason=str(exc))
                print(f'{i}/{len(links)} {url}: download failed, no cached copy ({exc})', file=sys.stderr)
    # Pages that disappeared from the index stay in the cache and history but are not loaded again.
    for url, entry in manifest.items():
        if url != INDEX and url not in links:
            entry['listed'] = False
    save_manifest()
    write_summary(manifest, links)
    fetch_supplier_pdf(session)


def fetch_supplier_pdf(session):
    import requests
    pdf = ROOT / 'data/raw/sf0828.pdf'
    status = ROOT / 'docs/results/supplier_download.json'
    if not pdf.exists():
        try:
            response = session.get(SUPPLIER_PDF, timeout=45)
            response.raise_for_status()
            if not response.content.startswith(b'%PDF'):
                raise RuntimeError('Supplier source is not a PDF')
            pdf.write_bytes(response.content)
        except (requests.RequestException, RuntimeError) as exc:
            status.write_bytes(json.dumps({'downloaded': False, 'reason': str(exc), 'source': SUPPLIER_PDF,
                'reference_values': 'Transcribed from Table 1 using a web PDF reader on 2026-10-05; see config/release_policy.yaml'}, indent=2).encode('utf-8'))
            print('Supplier PDF local download unavailable; cited reference transcription is retained.')
    if pdf.exists():
        status.write_bytes(json.dumps({'downloaded': True, 'sha256': hashlib.sha256(pdf.read_bytes()).hexdigest()}, indent=2).encode('utf-8'))


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--offline', action='store_true', help='Verify the local cache only; no network access')
    mode.add_argument('--refresh-norms', action='store_true', help='Re-download robots.txt, index and all norm pages')
    args = ap.parse_args()
    if args.offline:
        offline()
    else:
        download(args.refresh_norms)


if __name__ == '__main__':
    main()
