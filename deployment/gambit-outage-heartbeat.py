"""Empty, bounded external VPS heartbeat. Requires owner-configured free check.

No trading imports, secrets in stdout, redirects, retries, payload or wallet
access. A missing config leaves installation dormant until the owner signs in.
"""
import json
import os
import re
import stat
import urllib.request
from pathlib import Path

CONFIG = Path('/etc/gambit-outage-heartbeat.json')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def load_url(path=CONFIG):
    if path.is_symlink():
        raise ValueError('UNSAFE_CONFIG')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 1024:
        raise ValueError('UNSAFE_CONFIG')
    value = json.loads(path.read_text())
    url = value.get('ping_url')
    if not isinstance(url, str) or not re.fullmatch(
            r'https://hc-ping\.com/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', url):
        raise ValueError('INVALID_PING_DESTINATION')
    return url


def ping(url, opener=None):
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, data=b'', method='POST')
    with opener.open(request, timeout=5) as response:
        if response.status != 200:
            raise ValueError('HEARTBEAT_REJECTED')


def main():
    try:
        ping(load_url())
    except Exception:
        print('VPS_HEARTBEAT_FAILED', flush=True)
        return 1
    print('VPS_HEARTBEAT_DELIVERED', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
