#!/usr/bin/env python3
"""Read-only: fetch FUSSBALL.DE font-face CSS and associated font assets."""
import json, re, os
from pathlib import Path
from urllib.parse import urljoin
import requests

ROOT = Path('font-diagnose')
ROOT.mkdir(exist_ok=True)
PAGE = 'https://www.fussball.de/spiel/-/spiel-id/02V0TTVCC4000000VS5489B4VVE5F8E5'  # replaced by input if given
INPUT = os.environ.get('FUSSBALL_SPIEL_URL', '').strip()
if INPUT:
    PAGE = INPUT
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36', 'Accept': '*/*'})
# Source page from prior diagnosis is optionally embedded via file; otherwise use user-supplied page URL.
page_file = Path('spielseite.html')
if page_file.exists():
    html = page_file.read_text(encoding='utf-8')
    source = 'spielseite.html (bundled diagnosis)'
else:
    if not INPUT:
        raise SystemExit('FUSSBALL_SPIEL_URL is required if spielseite.html is absent')
    response = session.get(PAGE, timeout=30)
    response.raise_for_status()
    html = response.text
    source = response.url
ids = sorted(set(re.findall(r'data-obfuscation=["\']([A-Za-z0-9]+)["\']', html)))
(ROOT / 'meta.json').write_text(json.dumps({'source': source, 'ids': ids}, indent=2), encoding='utf-8')
print(f'Source: {source}; obfuscation IDs: {len(ids)}')
for ident in ids:
    css_url = f'https://www.fussball.de/export.fontface/-/id/{ident}/type/css'
    try:
        response = session.get(css_url, timeout=30)
        print(f'{ident}: CSS HTTP {response.status_code}, bytes {len(response.content)}')
        response.raise_for_status()
        css = response.text
        (ROOT / f'{ident}.css').write_text(css, encoding='utf-8')
        urls = re.findall(r'url\(\s*["\']?([^"\')]+)', css)
        for j, raw in enumerate(urls):
            asset_url = urljoin(css_url, raw)
            if not asset_url.startswith('https://www.fussball.de/'):
                print('Skip external font URL', asset_url)
                continue
            asset = session.get(asset_url, timeout=30)
            print(f'  font {j}: HTTP {asset.status_code}, bytes {len(asset.content)}')
            asset.raise_for_status()
            ext = 'woff2' if 'woff2' in raw else 'woff' if 'woff' in raw else 'ttf' if 'ttf' in raw else 'bin'
            (ROOT / f'{ident}_{j}.{ext}').write_bytes(asset.content)
    except requests.RequestException as exc:
        print(f'  WARNING: {ident}: {exc}')
print('Finished; files in font-diagnose/')
