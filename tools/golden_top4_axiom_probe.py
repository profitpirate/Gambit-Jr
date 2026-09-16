"""Bounded, anonymous first-party source discovery. NOT a trading test.

Only HTTPS GETs to Axiom public pages and their directly linked first-party JS.
No cookies, auth extraction, proxy rotation, challenge bypass or order methods.
A public HTML response does not verify a token, a quote, a feed or a fill.
"""
from __future__ import annotations
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

OUT = Path('artifacts/top4-axiom-preflight')
MAX_BODY = 2_000_000

def allowed(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    return p.scheme == 'https' and not p.username and not p.password and p.port in (None, 443) and (p.hostname == 'axiom.trade' or (p.hostname or '').endswith('.axiom.trade'))

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl):
            raise ValueError('non-first-party redirect refused')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def get(url: str):
    start = time.monotonic_ns()
    row = {'url': url, 'requested_ns': time.time_ns(), 'method': 'GET', 'authenticated': False}
    text = ''
    if not allowed(url):
        return {**row, 'error': 'URL_NOT_ALLOWED'}, text
    opener = urllib.request.build_opener(SafeRedirect())
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Gambit-Research-ReadOnly-Preflight/1.0', 'Accept': '*/*'})
        with opener.open(req, timeout=12) as response:
            body = response.read(MAX_BODY + 1)
            row.update(status=response.status, final_url=response.url,
                       content_type=response.headers.get('Content-Type', ''),
                       truncated=len(body) > MAX_BODY, bytes_read=len(body))
            body = body[:MAX_BODY]
            row['body_sha256'] = hashlib.sha256(body).hexdigest()
            text = body.decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        row.update(status=exc.code, error='HTTP_ERROR_NO_BYPASS_ATTEMPT')
    except Exception as exc:
        row['error'] = type(exc).__name__
    row['rtt_ms'] = (time.monotonic_ns() - start) / 1e6
    return row, text

class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(); self.urls = set()
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        value = a.get('src') if tag == 'script' else a.get('href') if tag == 'link' else None
        if value and '.js' in value:
            url = urllib.parse.urljoin('https://axiom.trade/', value)
            if allowed(url): self.urls.add(url)

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []; documents = []
    for url in ('https://axiom.trade/', 'https://axiom.trade/pulse', 'https://docs.axiom.trade/llms.txt'):
        row, text = get(url); rows.append(row); documents.append((url, text))
    scripts = Scripts()
    for url, text in documents[:2]: scripts.feed(text)
    urls = sorted(scripts.urls)[:32]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for row, text in pool.map(get, urls):
            rows.append(row); documents.append((row['url'], text))
    origins = {}; paths = {}; websocket_literals = {}
    for source, text in documents:
        for scheme, host in re.findall(r'(https?|wss?)://([a-zA-Z0-9.-]*axiom\.trade)(?=[/:\s\"\'\\]|$)', text):
            origin = scheme+'://'+host
            if host == 'axiom.trade' or host.endswith('.axiom.trade'):
                origins.setdefault(origin, []).append(source)
        # Only public endpoint names, not headers, cookies or tokens.
        for value in re.findall(r'[\"\'](/[a-zA-Z0-9_./-]{2,100})[\"\']', text):
            if any(word in value.lower() for word in ('pulse','pair','market','token-info')):
                paths.setdefault(value, []).append(source)
        for value in re.findall(r'wss://[a-zA-Z0-9.-]*axiom\.trade(?:/[a-zA-Z0-9_./-]*)?', text):
            websocket_literals.setdefault(value, []).append(source)
    result = {'status':'SOURCE_DISCOVERY_ONLY_NOT_FEED_VERIFIED', 'checked_ns':time.time_ns(),
        'source_commit':__import__('os').getenv('GITHUB_SHA'),
        'first_party_requests':rows, 'linked_script_count':len(scripts.urls),
        'public_origins':{k:sorted(set(v)) for k,v in origins.items()},
        'public_endpoint_path_literals':{k:sorted(set(v)) for k,v in paths.items()},
        'public_websocket_literals':{k:sorted(set(v)) for k,v in websocket_literals.items()},
        'axiom_feed_verified':False, 'axiom_coin_proofs':0, 'live_test_started':False,
        'trades':0, 'wallet_credentials_used':False,
        'note':'Successful page GETs or discovered endpoints are not market-data verification. No guessed API request, subscription, login or order was submitted.'}
    (OUT/'source-discovery.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps(result,sort_keys=True))

if __name__ == '__main__': main()
