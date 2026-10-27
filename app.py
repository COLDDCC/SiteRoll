#!/usr/bin/env python3
"""SiteRoll: dependency-free, local-only website and GSC monitor.

Web UI:      python app.py
Scheduled:   python app.py run            (incremental checks + weekly report)
Report only: python app.py report
"""
import argparse, base64, gzip, hashlib, io, ipaddress, json, os, secrets, socket, sqlite3, sys, threading, time, urllib.error, urllib.parse, urllib.request, webbrowser
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import xml.etree.ElementTree as ET

VERSION = '0.2.0'
ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True, mode=0o700)
DB = DATA / 'siteroll.db'
REPORTS = DATA / 'reports'
SCOPE = 'https://www.googleapis.com/auth/webmasters.readonly'
TOKEN_URL = 'https://oauth2.googleapis.com/token'

DAILY_SAFE_LIMIT = 1500          # per property per UTC day; Google allows 2000
DEFAULT_BUDGET = 200             # GSC inspections per site per incremental run
TECHNICAL_LIMIT = 500            # technical mode fetches every page, so keep it bounded
SITEMAP_MAX_URLS = 50_000
SITEMAP_MAX_FILES = 100
SITEMAP_MAX_BYTES = 50_000_000   # sitemap protocol limit (uncompressed)
RECHECK_NOT_INDEXED_DAYS = 3
RECHECK_INDEXED_DAYS = 14
RETRY_DELAYS = (2, 6, 15)        # seconds, for 429 / 5xx / network errors
FATAL_STATUS = (401, 403, 429)   # stop the site: auth, permission or quota problems
MAX_CONSECUTIVE_ERRORS = 5

LOCK = threading.Lock()
JOB = {'running': False, 'done': 0, 'total': 0, 'message': '尚未检查', 'run_ids': [], 'completed': 0}
OAUTH = {}
SA_TOKEN = {}
SESSION = secrets.token_urlsafe(32)
PORT = 8765


# ---------------------------------------------------------------- storage

@contextmanager
def db():
    """Open, commit on success, and always close (sqlite3's own context manager never closes)."""
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def init_db():
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS sites(id INTEGER PRIMARY KEY, url TEXT UNIQUE, sitemap TEXT, property TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, site_id INTEGER, at TEXT, mode TEXT, error TEXT DEFAULT '', total INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS results(run_id INTEGER, url TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS quota(property TEXT, day TEXT, n INTEGER, PRIMARY KEY(property,day));
        CREATE TABLE IF NOT EXISTS pages(site_id INTEGER, url TEXT, first_seen TEXT, last_seen TEXT, lastmod TEXT, PRIMARY KEY(site_id,url));
        CREATE INDEX IF NOT EXISTS results_run ON results(run_id);''')
        for table, cols in (('runs', ('discovered', 'planned', 'deferred', 'outside')), ('sites', ('sitemap_at',))):
            have = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
            for col in cols:
                if col not in have:
                    c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {"TEXT" if col == "sitemap_at" else "INTEGER"}')


init_db()


def col(row, name, default=None):
    return row[name] if name in row.keys() else default


# ---------------------------------------------------------------- result interpretation

def result_state(row, mode):
    if row.get('error'): return 'error'
    if mode == 'gsc':
        x = row.get('inspection', {}).get('indexStatusResult', {})
        if x.get('verdict') == 'PASS': return 'indexed'
        if x.get('verdict') in ('FAIL', 'NEUTRAL'): return 'not_indexed'
        return 'unknown'
    if row.get('http', 0) >= 400: return 'http_error'
    if row.get('noindex'): return 'noindex'
    if row.get('redirects'): return 'redirect'
    if row.get('canonical') and row['canonical'] != row.get('final'): return 'canonical'
    return 'ok'


def definite(state):
    return state not in ('error', 'unknown')


def history(c, site_id, mode, before_id=None):
    """All results of one site and mode, per URL, oldest first."""
    sql = 'SELECT r.url, r.data, u.at, u.id FROM results r JOIN runs u ON r.run_id=u.id WHERE u.site_id=? AND u.mode=?'
    args = [site_id, mode]
    if before_id is not None:
        sql += ' AND u.id<?'
        args.append(before_id)
    out = {}
    for r in c.execute(sql + ' ORDER BY u.id', args):
        data = json.loads(r['data'])
        out.setdefault(r['url'], []).append({'at': r['at'], 'run': r['id'], 'state': result_state(data, mode), 'data': data})
    return out


def last_definite(entries, before_at=None):
    for e in reversed(entries or []):
        if before_at is not None and e['at'] >= before_at: continue
        if definite(e['state']): return e
    return None


def run_report(c, run):
    """Summary of one run, compared with each URL's last definite result before it.

    Incremental runs only cover part of a site, so the comparison is per URL rather than
    against the previous run as a whole. Errors, unknowns and missing URLs never count as
    lost index.
    """
    mode = run['mode']
    current = {r['url']: json.loads(r['data']) for r in c.execute('SELECT * FROM results WHERE run_id=?', (run['id'],))}
    previous = c.execute('SELECT * FROM runs WHERE site_id=? AND mode=? AND id<? ORDER BY id DESC LIMIT 1', (run['site_id'], mode, run['id'])).fetchone()
    before = history(c, run['site_id'], mode, run['id']) if previous else {}
    counts = {'checked': 0, 'failed': 0, 'indexed': 0, 'not_indexed': 0, 'unknown': 0}
    changes = []
    for url, row in current.items():
        status = result_state(row, mode)
        if status == 'error': counts['failed'] += 1
        else:
            counts['checked'] += 1
            if status in ('indexed', 'not_indexed', 'unknown'): counts[status] += 1
        old_entry = last_definite(before.get(url))
        old = old_entry['state'] if old_entry else None
        change = None
        if previous and url not in before: change = 'first_checked'
        elif old and definite(status):
            if mode == 'gsc' and old != 'indexed' and status == 'indexed': change = 'newly_indexed'
            elif mode == 'gsc' and old == 'indexed' and status == 'not_indexed': change = 'lost_index'
            elif mode == 'technical' and old == 'ok' and status != 'ok': change = 'new_issue'
            elif mode == 'technical' and old != 'ok' and status == 'ok': change = 'resolved_issue'
            elif old != status: change = 'status_changed'
        if change: changes.append({'url': url, 'kind': change, 'before': old, 'after': status})
    discovered, planned = col(run, 'discovered'), col(run, 'planned')
    expected = planned if planned is not None else discovered
    return {'run': dict(run), 'counts': counts, 'discovered': discovered,
            'planned': planned, 'deferred': col(run, 'deferred'), 'outside': col(run, 'outside'),
            'unchecked': max(0, expected - len(current)) if expected is not None else None,
            'baseline': dict(previous) if previous else None, 'changes': changes}


FETCH_LABELS = {
    'SOFT_404': '软 404', 'NOT_FOUND': '404 未找到', 'ACCESS_DENIED': '需要授权（401）',
    'SERVER_ERROR': '服务器错误（5xx）', 'REDIRECT_ERROR': '重定向错误', 'ACCESS_FORBIDDEN': '禁止访问（403）',
    'BLOCKED_4XX': '其他 4xx 错误', 'INTERNAL_CRAWL_ERROR': 'Google 内部抓取错误', 'INVALID_URL': '网址无效',
}
CATEGORIES = {
    'indexed': '已收录',
    'likely_error': '可能出错：多半是网站配置问题，建议处理',
    'review': '需要人工判断：Google 与网站的意图不一致',
    'watch': '观察：已发现或已抓取但未编入，通常与内容质量、内链或时间有关，不建议批量改动',
    'expected': '通常正常：页面主动把规范网址指向别处',
    'failed': '查询失败：不代表未收录',
    'unknown': '状态未知：Google 没有返回明确结论',
}


def classify(url, data):
    """Deterministic (category, reason) for one GSC result, so humans and AI start from the same facts."""
    if data.get('error'): return 'failed', 'Google 查询失败'
    x = data.get('inspection', {}).get('indexStatusResult') or {}
    if not x: return 'unknown', '未返回索引状态'
    verdict = x.get('verdict')
    if verdict == 'PASS': return 'indexed', '已收录'
    fetch, idx = x.get('pageFetchState'), x.get('indexingState')
    gc, uc = x.get('googleCanonical'), x.get('userCanonical')
    if x.get('robotsTxtState') == 'DISALLOWED' or fetch == 'BLOCKED_ROBOTS_TXT' or idx == 'BLOCKED_BY_ROBOTS_TXT':
        return 'likely_error', 'robots.txt 阻止抓取'
    if idx in ('BLOCKED_BY_META_TAG', 'BLOCKED_BY_HTTP_HEADER'):
        return 'likely_error', 'sitemap 中的页面设置了 noindex'
    if fetch in FETCH_LABELS: return 'likely_error', FETCH_LABELS[fetch]
    if uc and gc and uc != gc: return 'review', 'Google 选择了与声明不同的规范网址'
    if uc and uc.rstrip('/') != url.rstrip('/'): return 'expected', '页面声明的规范网址是其他网址'
    if verdict in ('FAIL', 'NEUTRAL'): return 'watch', x.get('coverageState') or '未编入索引'
    return 'unknown', x.get('coverageState') or '未知状态'


# ---------------------------------------------------------------- fetching

def validate_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
        raise ValueError('请输入公开的 HTTP/HTTPS 网址（仅支持 80/443 端口）')
    for a in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(a[4][0]).is_global:
            raise ValueError('只支持公开网站，不允许访问本机或内网地址')
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args): return None


PUBLIC_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def fetch(url, limit=3_000_000):
    chain = []
    for _ in range(8):
        validate_url(url)
        req = urllib.request.Request(url, headers={'User-Agent': f'SiteRoll/{VERSION} (+local SEO monitor)'})
        try: r = PUBLIC_OPENER.open(req, timeout=15)
        except urllib.error.HTTPError as e: r = e
        code = r.code
        if code in (301, 302, 303, 307, 308) and r.headers.get('Location'):
            target = urllib.parse.urljoin(url, r.headers['Location'])
            chain.append({'status': code, 'from': url, 'to': target})
            r.close()
            url = target
            continue
        with r:
            body = r.read(limit + 1)
            if len(body) > limit: raise ValueError('响应过大，已停止读取')
            return code, url, dict(r.headers), body, chain
    raise ValueError('重定向次数过多')


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.robots, self.canonical, self.title, self.in_title = [], '', '', False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta' and (a.get('name') or '').lower() in ('robots', 'googlebot'): self.robots.append(a.get('content') or '')
        if tag == 'link' and 'canonical' in (a.get('rel') or '').lower().split(): self.canonical = a.get('href') or ''
        if tag == 'title': self.in_title = True

    def handle_endtag(self, tag):
        if tag == 'title': self.in_title = False

    def handle_data(self, data):
        if self.in_title: self.title += data


def technical(url):
    status, final, headers, body, chain = fetch(url)
    p = MetaParser()
    p.feed(body.decode('utf-8', 'replace'))
    robots = ', '.join(p.robots + [v for k, v in headers.items() if k.lower() == 'x-robots-tag'])
    return {'http': status, 'final': final, 'redirects': chain, 'robots': robots,
            'noindex': any(x in robots.lower().replace(',', ' ').split() for x in ('noindex', 'none')),
            'canonical': urllib.parse.urljoin(final, p.canonical) if p.canonical else '', 'title': p.title.strip()}


def bare_host(host):
    host = (host or '').lower().rstrip('.')
    return host[4:] if host.startswith('www.') else host


def host_allowed(host, site, prop=''):
    """Same site: www and non-www are interchangeable; a domain property also covers its subdomains."""
    host = (host or '').lower().rstrip('.')
    if host and bare_host(host) == bare_host(urllib.parse.urlsplit(site).hostname): return True
    if prop.startswith('sc-domain:'):
        domain = prop[len('sc-domain:'):].lower().rstrip('.')
        return host == domain or host.endswith('.' + domain)
    return False


def gunzip_if_needed(body, limit=SITEMAP_MAX_BYTES):
    if body[:2] != b'\x1f\x8b': return body
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as g:
        out = g.read(limit + 1)
    if len(out) > limit: raise ValueError('解压后的站点地图超过 50MB')
    return out


def sitemap_urls(sitemap, site, max_pages=SITEMAP_MAX_URLS, prop='', lastmods=None):
    pending, seen, urls, known = [sitemap], set(), [], set()
    while pending:
        u = pending.pop(0)
        if u in seen: continue
        seen.add(u)
        if len(seen) > SITEMAP_MAX_FILES: raise ValueError(f'站点地图超过 {SITEMAP_MAX_FILES} 个文件，请分批检查')
        code, final, _, body, _ = fetch(u, SITEMAP_MAX_BYTES)
        if code != 200: raise ValueError(f'站点地图返回 HTTP {code}：{u}')
        body = gunzip_if_needed(body)
        if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper(): raise ValueError('不支持带实体声明的 XML')
        try: root = ET.fromstring(body)
        except ET.ParseError: raise ValueError(f'站点地图不是有效的 XML：{u}') from None
        kind = root.tag.split('}')[-1]
        if kind not in ('urlset', 'sitemapindex'): raise ValueError('不是有效的 XML 站点地图')
        for item in root:
            fields = {x.tag.split('}')[-1]: (x.text or '').strip() for x in item}
            loc = fields.get('loc')
            if not loc: continue
            p = urllib.parse.urlsplit(loc)
            if p.scheme not in ('http', 'https') or not host_allowed(p.hostname, site, prop): continue
            if kind == 'sitemapindex': pending.append(loc)
            elif loc not in known:
                known.add(loc)
                urls.append(loc)
                if lastmods is not None and fields.get('lastmod'): lastmods[loc] = fields['lastmod']
                if len(urls) > max_pages: raise ValueError(f'超过本次 {max_pages} 页上限，请使用较小的子站点地图')
    if not urls: raise ValueError('站点地图中没有找到同一网站的网页')
    return urls


# ---------------------------------------------------------------- Google auth

class GoogleError(ValueError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    @property
    def transient(self):
        return self.status is None or self.status == 429 or self.status >= 500


def credentials():
    path = ROOT / 'credentials.json'
    if not path.exists(): raise ValueError('请先按 README 配置 credentials.json（桌面应用 OAuth 客户端）或服务账号')
    value = json.loads(path.read_text(encoding='utf-8'))
    if 'installed' not in value: raise ValueError('需要桌面应用类型的 OAuth credentials.json')
    return value['installed']


def service_account():
    """Service account key from SITEROLL_SERVICE_ACCOUNT (path or JSON text) or service-account.json."""
    env = os.environ.get('SITEROLL_SERVICE_ACCOUNT', '').strip()
    if env: raw = env if env.startswith('{') else Path(env).read_text(encoding='utf-8')
    elif (ROOT / 'service-account.json').exists(): raw = (ROOT / 'service-account.json').read_text(encoding='utf-8')
    else: return None
    value = json.loads(raw)
    if value.get('type') != 'service_account' or not value.get('private_key') or not value.get('client_email'):
        raise ValueError('服务账号 JSON 无效：需要 Google Cloud 下载的服务账号密钥')
    return value


def auth_mode():
    try:
        if service_account(): return 'service_account'
    except Exception: return 'invalid_service_account'
    return 'oauth' if (DATA / 'token.json').exists() else None


def _der(data, i):
    tag, n = data[i], data[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7f
        n = int.from_bytes(data[i:i + k], 'big')
        i += k
    return tag, data[i:i + n], i + n


def _der_items(data):
    items, i = [], 0
    while i < len(data):
        tag, value, i = _der(data, i)
        items.append((tag, value))
    return items


def rsa_private_key(pem):
    """(n, d) from a PKCS#8 or PKCS#1 PEM key, parsed with the standard library only."""
    der = base64.b64decode(''.join(l for l in pem.strip().splitlines() if not l.startswith('-----')))
    items = _der_items(_der(der, 0)[1])
    if len(items) == 3 and items[2][0] == 0x04:  # PKCS#8 wraps the PKCS#1 key in an OCTET STRING
        items = _der_items(_der(items[2][1], 0)[1])
    ints = [int.from_bytes(v, 'big') for t, v in items if t == 0x02]
    if len(ints) < 4: raise ValueError('无法读取服务账号私钥')
    return ints[1], ints[3]


def rsa_sha256_sign(pem, message):
    """RSASSA-PKCS1-v1_5 with SHA-256 (JWT RS256)."""
    n, d = rsa_private_key(pem)
    k = (n.bit_length() + 7) // 8
    digest = bytes.fromhex('3031300d060960864801650304020105000420') + hashlib.sha256(message).digest()
    em = b'\x00\x01' + b'\xff' * (k - len(digest) - 3) + b'\x00' + digest
    return pow(int.from_bytes(em, 'big'), d, n).to_bytes(k, 'big')


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=')


def service_account_token(sa):
    now = int(time.time())
    if SA_TOKEN.get('email') == sa['client_email'] and SA_TOKEN.get('expires_at', 0) > now + 60:
        return SA_TOKEN['access_token']
    header = _b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
    claims = _b64url(json.dumps({'iss': sa['client_email'], 'scope': SCOPE, 'aud': TOKEN_URL, 'iat': now, 'exp': now + 3600}).encode())
    signing_input = header + b'.' + claims
    assertion = signing_input + b'.' + _b64url(rsa_sha256_sign(sa['private_key'], signing_input))
    v = token_request({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion.decode()})
    SA_TOKEN.update(email=sa['client_email'], access_token=v['access_token'], expires_at=now + int(v.get('expires_in', 3600)))
    return v['access_token']


def save_token(value):
    path = DATA / 'token.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f: json.dump(value, f)


def token_request(params):
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        raise GoogleError(f'Google 授权失败 HTTP {e.code}，请重新连接或检查客户端/服务账号配置', e.code) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
        raise GoogleError(f'无法连接 Google 授权服务：{e}') from None


def access_token():
    sa = service_account()
    if sa: return service_account_token(sa)
    path = DATA / 'token.json'
    if not path.exists(): raise ValueError('尚未连接 Google，请先授权或配置服务账号')
    t = json.loads(path.read_text())
    if t.get('expires_at', 0) < time.time() + 60:
        cr = credentials()
        v = token_request({'client_id': cr['client_id'], 'client_secret': cr.get('client_secret', ''), 'refresh_token': t.get('refresh_token', ''), 'grant_type': 'refresh_token'})
        t.update(v)
        t['expires_at'] = time.time() + v['expires_in']
        save_token(t)
    return t['access_token']


def google_api(url, payload=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={'Authorization': 'Bearer ' + access_token(), 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=25) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        try: detail = str(json.load(e).get('error', {}).get('message', ''))[:200]
        except Exception: detail = ''
        hint = {401: '授权已失效，请重新连接', 403: '检查 API 是否启用，以及账号/服务账号是否已加入该 GSC 属性', 429: '配额或速率限制，请稍后重试'}.get(e.code, '请求失败')
        raise GoogleError(f'Google API HTTP {e.code}：{hint}' + (f'（{detail}）' if detail else ''), e.code) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
        raise GoogleError(f'无法连接 Google API：{e}') from None


def quota_used(prop):
    day = datetime.now(timezone.utc).date().isoformat()
    with db() as c:
        row = c.execute('SELECT n FROM quota WHERE property=? AND day=?', (prop, day)).fetchone()
    return row[0] if row else 0


def reserve_quota(prop):
    day = datetime.now(timezone.utc).date().isoformat()
    with db() as c:
        c.execute('INSERT OR IGNORE INTO quota VALUES(?,?,0)', (prop, day))
        if c.execute('SELECT n FROM quota WHERE property=? AND day=?', (prop, day)).fetchone()[0] >= DAILY_SAFE_LIMIT:
            raise ValueError(f'SiteRoll 当日安全上限 {DAILY_SAFE_LIMIT} 次已用完；其他工具也可能消耗 Google 配额')
        c.execute('UPDATE quota SET n=n+1 WHERE property=? AND day=?', (prop, day))


def inspect_url(url, prop):
    for attempt in range(len(RETRY_DELAYS) + 1):
        reserve_quota(prop)
        try:
            v = google_api('https://searchconsole.googleapis.com/v1/urlInspection/index:inspect', {'inspectionUrl': url, 'siteUrl': prop, 'languageCode': 'zh-CN'})
            return v.get('inspectionResult', {})
        except GoogleError as e:
            if e.transient and attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            raise


# ---------------------------------------------------------------- incremental planning

def parse_time(value):
    try:
        t = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def age_days(at, now):
    t = parse_time(at)
    return (now - t).total_seconds() / 86400 if t else float('inf')


def plan_gsc(urls, hist, lastmods=None, budget=None, full=False, now=None):
    """Choose which URLs to inspect this run, most useful first.

    1 new in sitemap, 2 last check failed/unknown, 3 not indexed (recheck after a few days),
    4 lastmod newer than last check, 5 indexed but stale. Fresh URLs are skipped unless full.
    Returns ([(url, reason)], deferred_count).
    """
    now = now or datetime.now(timezone.utc)
    lastmods = lastmods or {}
    ranked = []
    for i, url in enumerate(urls):
        entries = hist.get(url)
        if not entries:
            ranked.append(((0, i), url, 'new'))
            continue
        last = entries[-1]
        age = age_days(last['at'], now)
        modified, checked = parse_time(lastmods.get(url, '')), parse_time(last['at'])
        if not definite(last['state']): ranked.append(((1, -age), url, 'retry'))
        elif last['state'] == 'not_indexed' and age >= RECHECK_NOT_INDEXED_DAYS: ranked.append(((2, -age), url, 'not_indexed'))
        elif modified and checked and modified > checked: ranked.append(((3, -age), url, 'updated'))
        elif last['state'] == 'indexed' and age >= RECHECK_INDEXED_DAYS: ranked.append(((4, -age), url, 'stale'))
        elif full: ranked.append(((5, -age), url, 'full'))
    ranked.sort(key=lambda x: x[0])
    chosen = ranked if budget is None else ranked[:max(0, budget)]
    return [(url, reason) for _, url, reason in chosen], len(urls) - len(chosen)


def record_pages(site_id, urls, lastmods, at):
    with db() as c:
        c.executemany('INSERT INTO pages(site_id,url,first_seen,last_seen,lastmod) VALUES(?,?,?,?,?) '
                      'ON CONFLICT(site_id,url) DO UPDATE SET last_seen=excluded.last_seen, lastmod=COALESCE(excluded.lastmod,pages.lastmod)',
                      [(site_id, u, at, at, lastmods.get(u)) for u in urls])
        c.execute('UPDATE sites SET sitemap_at=? WHERE id=?', (at, site_id))


# ---------------------------------------------------------------- scanning

def scan(ids, mode, full=False, budget=DEFAULT_BUDGET, log=None):
    say = log or (lambda s: None)
    try:
        init_db()
        with db() as c: sites = [dict(c.execute('SELECT * FROM sites WHERE id=?', (i,)).fetchone()) for i in ids]
        for s in sites:
            now = datetime.now(timezone.utc).isoformat()
            with db() as c: run = c.execute('INSERT INTO runs(site_id,at,mode) VALUES(?,?,?)', (s['id'], now, mode)).lastrowid
            JOB.setdefault('run_ids', []).append(run)
            prop = s.get('property') or ''
            checked = failed = 0
            try:
                if mode == 'gsc' and not prop: raise ValueError('请先为网站选择 GSC 属性')
                lastmods = {}
                urls = sitemap_urls(s['sitemap'], s['url'], SITEMAP_MAX_URLS if mode == 'gsc' else TECHNICAL_LIMIT, prop=prop, lastmods=lastmods)
                record_pages(s['id'], urls, lastmods, now)
                discovered, outside = len(urls), 0
                if mode == 'gsc':
                    if not prop.startswith('sc-domain:'):
                        inside = [u for u in urls if u.startswith(prop)]
                        outside, urls = len(urls) - len(inside), inside
                    with db() as c: hist = history(c, s['id'], 'gsc')
                    limit = None if (full and budget == DEFAULT_BUDGET) or not budget else budget
                    selected, deferred = plan_gsc(urls, hist, lastmods, limit, full)
                    room = max(0, DAILY_SAFE_LIMIT - quota_used(prop))
                    if len(selected) > room:
                        deferred += len(selected) - room
                        selected = selected[:room]
                else:
                    selected, deferred = [(u, 'technical') for u in urls], 0
                with db() as c:
                    c.execute('UPDATE runs SET discovered=?, planned=?, deferred=?, outside=? WHERE id=?', (discovered, len(selected), deferred, outside, run))
                JOB['total'] += len(selected)
                consecutive = 0
                for u, reason in selected:
                    JOB['message'] = u
                    try:
                        result = technical(u) if mode == 'technical' else {'inspection': inspect_url(u, prop), 'reason': reason}
                        consecutive = 0
                    except Exception as e:
                        result = {'error': str(e)}
                        failed += 1
                        if mode == 'gsc':
                            consecutive += 1
                            with db() as c: c.execute('INSERT INTO results VALUES(?,?,?)', (run, u, json.dumps(result, ensure_ascii=False)))
                            JOB['done'] += 1
                            if not isinstance(e, GoogleError) or e.status in FATAL_STATUS or consecutive >= MAX_CONSECUTIVE_ERRORS:
                                raise ValueError('GSC 检查中断：' + str(e))
                            continue
                    else:
                        checked += 1
                    with db() as c: c.execute('INSERT INTO results VALUES(?,?,?)', (run, u, json.dumps(result, ensure_ascii=False)))
                    JOB['done'] += 1
                    if mode == 'gsc': time.sleep(.2)
                with db() as c: c.execute('UPDATE runs SET total=? WHERE id=?', (len(selected), run))
                say(f'{s["url"]} · {"收录" if mode == "gsc" else "配置"}：发现 {discovered}，本次检查 {len(selected)}，跳过 {deferred}' + (f'，不属于该属性 {outside}' if outside else '') + (f'，失败 {failed}' if failed else ''))
            except Exception as e:
                with db() as c: c.execute('UPDATE runs SET error=? WHERE id=?', (str(e), run))
                say(f'{s["url"]} · {"收录" if mode == "gsc" else "配置"}：{e}')
        JOB['message'] = '检查完成；请查看结果和检查失败提示'
    except Exception as e:
        JOB['message'] = str(e)
        say(str(e))
    finally:
        JOB['running'] = False
        JOB['completed'] = JOB.get('completed', 0) + 1


def save_site(url, sitemap='', prop=''):
    url = validate_url(url.strip()).rstrip('/') + '/'
    prop = (prop or '').strip()
    if prop and not prop.startswith(('sc-domain:', 'https://', 'http://')): raise ValueError('GSC 属性格式错误')
    sitemap = validate_url((sitemap or '').strip() or urllib.parse.urljoin(url, 'sitemap.xml'))
    if not host_allowed(urllib.parse.urlsplit(sitemap).hostname, url, prop):
        raise ValueError('站点地图必须属于同一网站（www 与非 www 视为同一网站；域名属性包含子域名）')
    with db() as c:
        c.execute('INSERT INTO sites(url,sitemap,property) VALUES(?,?,?) ON CONFLICT(url) DO UPDATE SET sitemap=excluded.sitemap,property=excluded.property', (url, sitemap, prop))
    return url


def delete_site(site_id):
    with db() as c:
        c.execute('DELETE FROM results WHERE run_id IN(SELECT id FROM runs WHERE site_id=?)', (site_id,))
        c.execute('DELETE FROM runs WHERE site_id=?', (site_id,))
        c.execute('DELETE FROM pages WHERE site_id=?', (site_id,))
        c.execute('DELETE FROM sites WHERE id=?', (site_id,))


# ---------------------------------------------------------------- weekly report

def _examples(urls, n=50):
    return {'count': len(urls), 'urls': urls[:n]}


def site_week(c, s, since):
    sid = s['id']
    pages = [dict(r) for r in c.execute('SELECT * FROM pages WHERE site_id=? AND last_seen=?', (sid, col(s, 'sitemap_at'))) ] if col(s, 'sitemap_at') else []
    hist = history(c, sid, 'gsc')
    urls = [p['url'] for p in pages] or list(hist)
    categories, groups, pending, changes = {k: 0 for k in CATEGORIES}, {}, [], {'newly_indexed': [], 'lost_index': [], 'first_result': []}
    for url in urls:
        entries = hist.get(url)
        if not entries:
            pending.append(url)
            continue
        cat, reason = classify(url, entries[-1]['data'])
        categories[cat] += 1
        if cat != 'indexed': groups.setdefault(cat, {}).setdefault(reason, []).append(url)
        after, before = last_definite(entries), last_definite(entries, since)
        if after and after['at'] >= since:
            if before and before['state'] != after['state']:
                changes['newly_indexed' if after['state'] == 'indexed' else 'lost_index'].append(url)
            elif not before: changes['first_result'].append(url)
    removed = [r['url'] for r in c.execute('SELECT url FROM pages WHERE site_id=? AND last_seen<? AND last_seen>=?', (sid, col(s, 'sitemap_at') or '', since))]
    added = [p['url'] for p in pages if p['first_seen'] >= since]
    runs = [dict(r) for r in c.execute('SELECT id,at,mode,error,discovered,planned,deferred,outside FROM runs WHERE site_id=? AND at>=? ORDER BY id', (sid, since))]
    last_gsc = c.execute("SELECT at FROM runs WHERE site_id=? AND mode='gsc' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    tech = {}
    last_tech = c.execute("SELECT id,at FROM runs WHERE site_id=? AND mode='technical' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    if last_tech:
        for r in c.execute('SELECT url,data FROM results WHERE run_id=?', (last_tech['id'],)):
            state = result_state(json.loads(r['data']), 'technical')
            if state != 'ok': tech.setdefault(state, []).append(r['url'])
    errors = [{'at': r['at'], 'mode': r['mode'], 'error': r['error']} for r in runs if r['error']]
    attention = []
    if changes['lost_index']: attention.append(f'{len(changes["lost_index"])} 个网址失去收录')
    if categories['likely_error']: attention.append(f'{categories["likely_error"]} 个网址可能有配置问题')
    if errors: attention.append(f'本周有 {len(errors)} 次检查出错')
    if not s.get('property'): attention.append('未设置 GSC 属性，无法查询收录')
    elif not last_gsc or last_gsc['at'] < since: attention.append('本周没有新的 Google 收录数据')
    if categories['review']: attention.append(f'{categories["review"]} 个网址需要人工判断规范网址')
    return {
        'url': s['url'], 'property': s.get('property') or '', 'sitemap': s['sitemap'],
        'needs_attention': bool(attention), 'attention': attention,
        'sitemap_pages': len(pages) if pages else None,
        'categories': categories,
        'not_indexed_by_reason': {cat: {reason: _examples(u) for reason, u in sorted(g.items(), key=lambda x: -len(x[1]))} for cat, g in groups.items()},
        'pending_first_check': _examples(pending),
        'changes': {k: _examples(v) for k, v in changes.items()},
        'sitemap_added': _examples(added), 'sitemap_removed': _examples(removed),
        'technical_issues': {k: _examples(v, 20) for k, v in tech.items()},
        'technical_checked_at': last_tech['at'] if last_tech else None,
        'last_gsc_at': last_gsc['at'] if last_gsc else None,
        'quota_used_today': quota_used(s['property']) if s.get('property') else 0,
        'runs': runs, 'errors': errors,
    }


def weekly_report(days=7, now=None):
    init_db()
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    with db() as c:
        sites = [site_week(c, dict(s), since) for s in c.execute('SELECT * FROM sites ORDER BY id')]
    total = {k: sum(s['categories'][k] for s in sites) for k in CATEGORIES}
    sites.sort(key=lambda s: (not s['needs_attention'], -len(s['attention']), s['url']))
    return {
        'tool': 'SiteRoll', 'version': VERSION, 'generated_at': now.isoformat(),
        'period': {'since': since, 'until': now.isoformat(), 'days': days},
        'summary': {
            'sites': len(sites), 'sites_need_attention': sum(s['needs_attention'] for s in sites),
            'categories': total,
            'pending_first_check': sum(s['pending_first_check']['count'] for s in sites),
            'newly_indexed': sum(s['changes']['newly_indexed']['count'] for s in sites),
            'lost_index': sum(s['changes']['lost_index']['count'] for s in sites),
        },
        'legend': CATEGORIES,
        'notes': [
            '分类基于每个网址最近一次 Google URL Inspection 记录；查询失败和未知不等于未收录。',
            '变化只比较统计区间前后两次都有明确结论的网址。',
            'pending_first_check 是 sitemap 中尚未查询过 Google 的网址，通常是新页面。',
            'watch 类（已抓取/已发现但未编入）多与内容质量、内链和时间有关，不建议自动批量改动。',
            'technical_issues 是网站配置检查提示，HTTP/WWW 跳转、反馈页 noindex 等可能是正常设计。',
        ],
        'sites': sites,
    }


def weekly_markdown(r):
    s = r['summary']
    cat = s['categories']
    lines = [f'# SiteRoll 周报 · {r["generated_at"][:10]}', '',
             f'统计区间：{r["period"]["since"][:10]} 至 {r["period"]["until"][:10]}（{r["period"]["days"]} 天）', '',
             '## 总览', '',
             f'- 网站 {s["sites"]} 个，**需要关注 {s["sites_need_attention"]} 个**',
             f'- 本周变化：新增收录 {s["newly_indexed"]}，失去收录 {s["lost_index"]}',
             f'- 已收录 {cat["indexed"]} · 可能出错 {cat["likely_error"]} · 需人工判断 {cat["review"]} · 观察 {cat["watch"]} · 正常排除 {cat["expected"]} · 失败 {cat["failed"]} · 未知 {cat["unknown"]}',
             f'- sitemap 中待首次检查的网址：{s["pending_first_check"]}', '']
    flagged = [x for x in r['sites'] if x['needs_attention']]
    if flagged:
        lines += ['## 需要关注', '']
        for x in flagged:
            lines += [f'### {x["url"]}', '', *[f'- {a}' for a in x['attention']]]
            for kind, label in (('lost_index', '失去收录'), ('newly_indexed', '新增收录')):
                ch = x['changes'][kind]
                if ch['count']: lines.append(f'- {label} {ch["count"]}：' + '、'.join(ch['urls'][:5]) + (' 等' if ch['count'] > 5 else ''))
            for c in ('likely_error', 'review'):
                for reason, g in x['not_indexed_by_reason'].get(c, {}).items():
                    lines.append(f'- {reason}（{g["count"]}）：' + '、'.join(g['urls'][:5]) + (' 等' if g['count'] > 5 else ''))
            for e in x['errors'][:3]: lines.append(f'- 检查出错（{e["at"][:16]}）：{e["error"]}')
            lines.append('')
    lines += ['## 所有网站', '', '| 网站 | 已收录 | 可能出错 | 需判断 | 观察 | 待首查 | 本周 +/- | 最近收录检查 |', '|---|---|---|---|---|---|---|---|']
    for x in r['sites']:
        c = x['categories']
        lines.append(f'| {x["url"]} | {c["indexed"]} | {c["likely_error"]} | {c["review"]} | {c["watch"]} | {x["pending_first_check"]["count"]} | +{x["changes"]["newly_indexed"]["count"]} / -{x["changes"]["lost_index"]["count"]} | {(x["last_gsc_at"] or "从未")[:10]} |')
    lines += ['', '> 分类说明：' + '；'.join(f'{k}＝{v}' for k, v in r['legend'].items()), '']
    return '\n'.join(lines)


def write_weekly(days=7, out=None):
    out = Path(out) if out else REPORTS
    out.mkdir(parents=True, exist_ok=True)
    report = weekly_report(days)
    md = weekly_markdown(report)
    stamp = report['generated_at'][:10]
    paths = []
    for name in (f'weekly-{stamp}', 'latest'):
        (out / f'{name}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (out / f'{name}.md').write_text(md, encoding='utf-8')
        paths += [out / f'{name}.json', out / f'{name}.md']
    return report, md, paths


# ---------------------------------------------------------------- web UI

def snapshot():
    with db() as c:
        sites = [dict(r) for r in c.execute('SELECT * FROM sites ORDER BY id')]
        for s in sites:
            s['runs'] = [dict(r) for r in c.execute('SELECT * FROM runs WHERE site_id=? ORDER BY id DESC LIMIT 12', (s['id'],))]
            latest = c.execute("SELECT * FROM runs WHERE site_id=? AND mode='gsc' ORDER BY id DESC LIMIT 1", (s['id'],)).fetchone()
            s['index_summary'] = run_report(c, latest) if latest else None
    auth = auth_mode()
    return {'sites': sites, 'job': JOB.copy(), 'auth': auth, 'connected': auth in ('oauth', 'service_account'),
            'configured': (ROOT / 'credentials.json').exists(), 'version': VERSION}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # OAuth codes must never appear in logs.

    def trusted(self): return self.headers.get('Host') in (f'127.0.0.1:{PORT}', f'localhost:{PORT}')

    def send(self, status, value, kind='application/json'):
        raw = (json.dumps(value, ensure_ascii=False) if kind == 'application/json' else value).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind + '; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.trusted(): return self.send(403, {'error': 'Invalid Host'})
        p = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(p.query)
        try:
            if p.path == '/': return self.send(200, (ROOT / 'web/index.html').read_text(encoding='utf-8').replace('__SESSION__', SESSION), 'text/html')
            if p.path == '/oauth/callback':
                state = q.get('state', [''])[0]
                with LOCK: pending = OAUTH.pop(state, None)
                if not pending or time.time() - pending['at'] > 600: raise ValueError('授权请求失效，请返回工具重新连接')
                if 'error' in q: raise ValueError('Google 授权未完成')
                cr = credentials()
                v = token_request({'code': q.get('code', [''])[0], 'client_id': cr['client_id'], 'client_secret': cr.get('client_secret', ''), 'redirect_uri': f'http://127.0.0.1:{PORT}/oauth/callback', 'grant_type': 'authorization_code', 'code_verifier': pending['verifier']})
                v['expires_at'] = time.time() + v['expires_in']
                save_token(v)
                return self.send(200, '<meta charset="utf-8"><p>Google 已连接。关闭此页，返回 SiteRoll 刷新并导入网站。</p>', 'text/html')
            if self.headers.get('X-SiteRoll-Token') != SESSION: return self.send(403, {'error': '请从本地首页访问'})
            if p.path == '/api/state': return self.send(200, snapshot())
            if p.path == '/api/report':
                with db() as c:
                    run = c.execute('SELECT * FROM runs WHERE id=?', (int(q['run'][0]),)).fetchone()
                    if not run: raise ValueError('检查记录不存在')
                    report = run_report(c, run)
                return self.send(200, report)
            if p.path == '/api/results':
                with db() as c: rows = [{'url': r['url'], **json.loads(r['data'])} for r in c.execute('SELECT * FROM results WHERE run_id=?', (int(q['run'][0]),))]
                return self.send(200, rows)
            if p.path == '/api/properties': return self.send(200, google_api('https://www.googleapis.com/webmasters/v3/sites').get('siteEntry', []))
            self.send(404, {'error': 'Not found'})
        except Exception as e: self.send(400, {'error': str(e)})

    def do_POST(self):
        if not self.trusted() or self.headers.get('X-SiteRoll-Token') != SESSION: return self.send(403, {'error': 'Forbidden'})
        origin = self.headers.get('Origin')
        if origin not in (None, f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'): return self.send(403, {'error': 'Invalid Origin'})
        try:
            size = int(self.headers.get('Content-Length', 0))
            if size > 100_000: raise ValueError('请求过大')
            v = json.loads(self.rfile.read(size) or b'{}')
            if self.path == '/api/sites':
                save_site(v['url'], v.get('sitemap', ''), v.get('property', ''))
                return self.send(200, {'ok': True})
            if self.path == '/api/delete':
                if JOB['running']: raise ValueError('检查期间不能删除网站')
                delete_site(v['id'])
                return self.send(200, {'ok': True})
            if self.path == '/api/scan':
                with LOCK:
                    if JOB['running']: raise ValueError('已有检查正在运行')
                    if v.get('mode') not in ('technical', 'gsc'): raise ValueError('检查模式错误')
                    if not v.get('ids'): raise ValueError('请先添加网站')
                    JOB.update(running=True, done=0, total=0, message='正在读取 sitemap', run_ids=[])
                    threading.Thread(target=scan, args=(v['ids'], v['mode'], bool(v.get('full'))), daemon=True).start()
                return self.send(200, {'ok': True})
            if self.path == '/api/weekly':
                report, md, paths = write_weekly(int(v.get('days') or 7))
                return self.send(200, {'markdown': md, 'summary': report['summary'], 'paths': [str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p) for p in paths]})
            if self.path == '/api/connect':
                cr = credentials()
                state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
                with LOCK: OAUTH[state] = {'verifier': verifier, 'at': time.time()}
                params = {'client_id': cr['client_id'], 'redirect_uri': f'http://127.0.0.1:{PORT}/oauth/callback', 'response_type': 'code', 'scope': SCOPE, 'access_type': 'offline', 'prompt': 'consent', 'state': state, 'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='), 'code_challenge_method': 'S256'}
                return self.send(200, {'url': 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode(params)})
            if self.path == '/api/disconnect':
                if JOB['running']: raise ValueError('请等待检查完成')
                (DATA / 'token.json').unlink(missing_ok=True)
                return self.send(200, {'ok': True})
            self.send(404, {'error': 'Not found'})
        except Exception as e: self.send(400, {'error': str(e)})


# ---------------------------------------------------------------- command line

def cli_run(args):
    init_db()
    with db() as c: sites = [dict(r) for r in c.execute('SELECT * FROM sites ORDER BY id')]
    if args.site:
        wanted = {bare_host(urllib.parse.urlsplit(s if '://' in s else 'https://' + s).hostname) for s in args.site}
        sites = [s for s in sites if bare_host(urllib.parse.urlsplit(s['url']).hostname) in wanted]
    if not sites:
        print('没有匹配的网站。先在网页里添加，或使用：python app.py add-site https://example.com/')
        return 1
    modes = ['technical', 'gsc'] if args.mode == 'all' else [args.mode]
    for mode in modes:
        ids = [s['id'] for s in sites if mode == 'technical' or s['property']]
        if mode == 'gsc':
            skipped = [s['url'] for s in sites if not s['property']]
            if skipped: print('未设置 GSC 属性，跳过收录检查：' + '、'.join(skipped))
            if ids and auth_mode() not in ('oauth', 'service_account'):
                print('未连接 Google（OAuth 或服务账号），跳过收录检查。')
                ids = []
        if not ids: continue
        print(f'== {"Google 收录（" + ("全量" if args.full else "增量") + "）" if mode == "gsc" else "网站配置"}：{len(ids)} 个网站')
        JOB.update(running=True, done=0, total=0, message='', run_ids=[])
        scan(ids, mode, full=args.full, budget=args.budget, log=lambda m: print('  ' + m, flush=True))
    if not args.no_report:
        report, _, paths = write_weekly(args.days, args.out)
        s = report['summary']
        print(f'周报：{paths[1]}（需要关注 {s["sites_need_attention"]} 个网站，新增收录 {s["newly_indexed"]}，失去收录 {s["lost_index"]}）')
    return 0


def main(argv=None):
    global PORT
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(errors='replace')
        except Exception: pass
    parser = argparse.ArgumentParser(prog='app.py', description='SiteRoll：多网站 Google 收录监控')
    parser.add_argument('--port', type=int, default=8765, help='网页端口（默认 8765）')
    parser.add_argument('--no-browser', action='store_true', help='启动网页时不自动打开浏览器')
    sub = parser.add_subparsers(dest='cmd')
    r = sub.add_parser('run', help='检查后生成周报，适合任务计划程序 / cron')
    r.add_argument('--mode', choices=['all', 'gsc', 'technical'], default='all')
    r.add_argument('--full', action='store_true', help='忽略增量规则，检查全部网址（仍受配额限制）')
    r.add_argument('--budget', type=int, default=DEFAULT_BUDGET, help=f'每站每次最多查询 Google 的网址数，0 表示不限（默认 {DEFAULT_BUDGET}）')
    r.add_argument('--site', action='append', help='只检查指定网站，可重复')
    r.add_argument('--days', type=int, default=7, help='周报统计天数')
    r.add_argument('--out', help='周报输出目录（默认 data/reports）')
    r.add_argument('--no-report', action='store_true')
    w = sub.add_parser('report', help='只根据已有记录生成周报')
    w.add_argument('--days', type=int, default=7)
    w.add_argument('--out')
    a = sub.add_parser('add-site', help='添加或更新网站')
    a.add_argument('url')
    a.add_argument('--sitemap', default='')
    a.add_argument('--property', default='')
    sub.add_parser('sites', help='列出网站')
    args = parser.parse_args(argv)
    PORT = args.port
    if args.cmd == 'run': return cli_run(args)
    if args.cmd == 'report':
        report, _, paths = write_weekly(args.days, args.out)
        print(f'周报：{paths[1]}（需要关注 {report["summary"]["sites_need_attention"]} 个网站）')
        return 0
    if args.cmd == 'add-site':
        print('已保存：' + save_site(args.url, args.sitemap, args.property))
        return 0
    if args.cmd == 'sites':
        with db() as c:
            for s in c.execute('SELECT * FROM sites ORDER BY id'): print(f'{s["id"]}\t{s["url"]}\t{s["property"] or "-"}\t{s["sitemap"]}')
        return 0
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'SiteRoll {VERSION}: http://127.0.0.1:{PORT} — Ctrl+C 停止。数据保存在 data/，请勿提交。')
    if not args.no_browser: webbrowser.open(f'http://127.0.0.1:{PORT}')
    try: server.serve_forever()
    except KeyboardInterrupt: server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
