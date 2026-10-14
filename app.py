#!/usr/bin/env python3
"""SiteRoll: dependency-free, local-only website and GSC monitor."""
import argparse, base64, csv, hashlib, io, ipaddress, json, os, secrets, socket, sqlite3, threading, time, urllib.error, urllib.parse, urllib.request, webbrowser
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True, mode=0o700)
DB = DATA / 'siteroll.db'
LOCK = threading.Lock()
JOB = {'running': False, 'done': 0, 'total': 0, 'message': '尚未检查', 'run_ids': [], 'completed': 0}
OAUTH = {}
SESSION = secrets.token_urlsafe(32)
PORT = 8765

def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c
with db() as c:
    c.executescript('''CREATE TABLE IF NOT EXISTS sites(id INTEGER PRIMARY KEY, url TEXT UNIQUE, sitemap TEXT, property TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, site_id INTEGER, at TEXT, mode TEXT, error TEXT DEFAULT '', total INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS results(run_id INTEGER, url TEXT, data TEXT);
    CREATE TABLE IF NOT EXISTS quota(property TEXT, day TEXT, n INTEGER, PRIMARY KEY(property,day));''')

    columns = {r[1] for r in c.execute('PRAGMA table_info(runs)')}
    if 'discovered' not in columns:
        c.execute('ALTER TABLE runs ADD COLUMN discovered INTEGER')

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

def run_report(c, run):
    current = {r['url']: json.loads(r['data']) for r in c.execute('SELECT * FROM results WHERE run_id=?', (run['id'],))}
    previous = c.execute('SELECT * FROM runs WHERE site_id=? AND mode=? AND id<? ORDER BY id DESC LIMIT 1', (run['site_id'], run['mode'], run['id'])).fetchone()
    before = {r['url']: json.loads(r['data']) for r in c.execute('SELECT * FROM results WHERE run_id=?', (previous['id'],))} if previous else {}
    counts = {'checked': 0, 'failed': 0, 'indexed': 0, 'not_indexed': 0, 'unknown': 0}
    changes = []
    for url, row in current.items():
        status = result_state(row, run['mode'])
        if status == 'error': counts['failed'] += 1
        else:
            counts['checked'] += 1
            if status in ('indexed', 'not_indexed', 'unknown'): counts[status] += 1
        old = result_state(before[url], run['mode']) if url in before else None
        change = None
        if previous and old is None: change = 'first_checked'
        elif old and status not in ('error', 'unknown') and old not in ('error', 'unknown'):
            if run['mode'] == 'gsc' and old != 'indexed' and status == 'indexed': change = 'newly_indexed'
            elif run['mode'] == 'gsc' and old == 'indexed' and status == 'not_indexed': change = 'lost_index'
            elif run['mode'] == 'technical' and old == 'ok' and status != 'ok': change = 'new_issue'
            elif run['mode'] == 'technical' and old != 'ok' and status == 'ok': change = 'resolved_issue'
            elif old != status: change = 'status_changed'
        if change: changes.append({'url': url, 'kind': change, 'before': old, 'after': status})
    discovered = run['discovered']
    return {'run': dict(run), 'counts': counts, 'discovered': discovered,
            'unchecked': max(0, discovered - len(current)) if discovered is not None else None,
            'baseline': dict(previous) if previous else None, 'changes': changes}

def validate_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.port not in (None,80,443):
        raise ValueError('请输入公开的 HTTP/HTTPS 网址（仅支持 80/443 端口）')
    for a in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme=='https' else 80), type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(a[4][0]).is_global:
            raise ValueError('只支持公开网站，不允许访问本机或内网地址')
    return url

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): return None
PUBLIC_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

def fetch(url, limit=3_000_000):
    chain=[]
    for _ in range(8):
        validate_url(url)
        req=urllib.request.Request(url,headers={'User-Agent':'SiteRoll/0.1 (+local SEO monitor)'})
        try: r=PUBLIC_OPENER.open(req,timeout=15)
        except urllib.error.HTTPError as e: r=e
        code=r.code
        if code in (301,302,303,307,308) and r.headers.get('Location'):
            target=urllib.parse.urljoin(url,r.headers['Location']); chain.append({'status':code,'from':url,'to':target});r.close();url=target;continue
        with r:
            body=r.read(limit+1)
            if len(body)>limit: raise ValueError('响应过大，已停止读取')
            return code,url,dict(r.headers),body,chain
    raise ValueError('重定向次数过多')

class MetaParser(HTMLParser):
    def __init__(self): super().__init__();self.robots=[];self.canonical='';self.title='';self.in_title=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='meta' and a.get('name','').lower() in ('robots','googlebot'): self.robots.append(a.get('content',''))
        if tag=='link' and 'canonical' in a.get('rel','').lower().split():self.canonical=a.get('href','')
        if tag=='title':self.in_title=True
    def handle_endtag(self,tag):
        if tag=='title':self.in_title=False
    def handle_data(self,data):
        if self.in_title:self.title+=data

def technical(url):
    status,final,headers,body,chain=fetch(url)
    p=MetaParser();p.feed(body.decode('utf-8','replace'))
    robots=', '.join(p.robots+[v for k,v in headers.items() if k.lower()=='x-robots-tag'])
    return {'http':status,'final':final,'redirects':chain,'robots':robots,'noindex':any(x in robots.lower().replace(',',' ').split() for x in ('noindex','none')),'canonical':urllib.parse.urljoin(final,p.canonical) if p.canonical else '', 'title':p.title.strip()}

def sitemap_urls(sitemap, site, max_pages=500):
    pending=[sitemap];seen=set();urls=[]; origin=urllib.parse.urlsplit(site).netloc
    while pending:
        u=pending.pop(0)
        if u in seen:continue
        seen.add(u)
        if len(seen)>30:raise ValueError('站点地图超过 30 个文件，请分批检查')
        code,final,_,body,_=fetch(u)
        if code!=200:raise ValueError(f'站点地图返回 HTTP {code}')
        if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():raise ValueError('不支持带实体声明的 XML')
        root=ET.fromstring(body)
        kind=root.tag.split('}')[-1]
        if kind not in ('urlset','sitemapindex'):raise ValueError('不是有效的 XML 站点地图')
        for item in root:
            loc=next((x.text.strip() for x in item if x.tag.split('}')[-1]=='loc' and x.text),None)
            if not loc:continue
            p=urllib.parse.urlsplit(loc)
            if p.scheme not in ('http','https') or p.netloc!=origin:continue
            if kind=='sitemapindex':pending.append(loc)
            elif loc not in urls:
                urls.append(loc)
                if len(urls)>max_pages:raise ValueError(f'超过本次 {max_pages} 页上限，请使用较小的子站点地图')
    if not urls:raise ValueError('站点地图中没有找到同域名网页')
    return urls

def credentials():
    path=ROOT/'credentials.json'
    if not path.exists():raise ValueError('请先按 README 配置 credentials.json（桌面应用 OAuth 客户端）')
    value=json.loads(path.read_text())
    if 'installed' not in value:raise ValueError('需要桌面应用类型的 OAuth credentials.json')
    return value['installed']

def save_token(value):
    path=DATA/'token.json'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f)

def token_request(params):
    req=urllib.request.Request('https://oauth2.googleapis.com/token',data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)
    except urllib.error.HTTPError as e:raise ValueError(f'Google 授权失败 HTTP {e.code}，请重新连接或检查客户端配置') from None

def access_token():
    path=DATA/'token.json'
    if not path.exists():raise ValueError('尚未连接 Google，请先授权')
    t=json.loads(path.read_text())
    if t.get('expires_at',0)<time.time()+60:
        cr=credentials();v=token_request({'client_id':cr['client_id'],'client_secret':cr.get('client_secret',''),'refresh_token':t.get('refresh_token',''),'grant_type':'refresh_token'})
        t.update(v);t['expires_at']=time.time()+v['expires_in'];save_token(t)
    return t['access_token']

def google_api(url,payload=None):
    req=urllib.request.Request(url,data=json.dumps(payload).encode() if payload is not None else None,headers={'Authorization':'Bearer '+access_token(),'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=25) as r:return json.load(r)
    except urllib.error.HTTPError as e:
        raise ValueError(f'Google API HTTP {e.code}：'+{401:'授权已失效，请重新连接',403:'检查 API 是否启用及网站访问权限',429:'配额或速率限制，请稍后重试'}.get(e.code,'请求失败，请稍后重试')) from None

def inspect_url(url,prop):
    day=datetime.now(timezone.utc).date().isoformat()
    with db() as c:
        c.execute('INSERT OR IGNORE INTO quota VALUES(?,?,0)',(prop,day))
        n=c.execute('SELECT n FROM quota WHERE property=? AND day=?',(prop,day)).fetchone()[0]
        if n>=1500:raise ValueError('SiteRoll 当日安全上限 1500 次已用完；其他工具也可能消耗 Google 配额')
        c.execute('UPDATE quota SET n=n+1 WHERE property=? AND day=?',(prop,day))
    v=google_api('https://searchconsole.googleapis.com/v1/urlInspection/index:inspect',{'inspectionUrl':url,'siteUrl':prop,'languageCode':'zh-CN'})
    return v.get('inspectionResult',{})

def scan(ids,mode):
    try:
        with db() as c:sites=[dict(c.execute('SELECT * FROM sites WHERE id=?',(i,)).fetchone()) for i in ids]
        for s in sites:
            now=datetime.now(timezone.utc).isoformat()
            with db() as c:run=c.execute('INSERT INTO runs(site_id,at,mode) VALUES(?,?,?)',(s['id'],now,mode)).lastrowid
            JOB.setdefault('run_ids', []).append(run)
            try:
                if mode=='gsc' and not s['property']:raise ValueError('请先为网站选择 GSC 属性')
                urls=sitemap_urls(s['sitemap'],s['url']);JOB['total']+=len(urls)
                with db() as c:c.execute('UPDATE runs SET discovered=? WHERE id=?',(len(urls),run))
                for u in urls:
                    JOB['message']=u
                    try:
                        result=technical(u) if mode=='technical' else {'inspection':inspect_url(u,s['property'])}
                    except Exception as e:
                        result={'error':str(e)}
                        if mode=='gsc':
                            with db() as c:c.execute('INSERT INTO results VALUES(?,?,?)',(run,u,json.dumps(result,ensure_ascii=False)))
                            JOB['done']+=1
                            raise ValueError('GSC 检查中断：'+str(e))
                    with db() as c:c.execute('INSERT INTO results VALUES(?,?,?)',(run,u,json.dumps(result,ensure_ascii=False)))
                    JOB['done']+=1
                    if mode=='gsc':time.sleep(.2)
                with db() as c:c.execute('UPDATE runs SET total=? WHERE id=?',(len(urls),run))
            except Exception as e:
                with db() as c:c.execute('UPDATE runs SET error=? WHERE id=?',(str(e),run))
        JOB['message']='检查完成；请查看结果和检查失败提示'
    except Exception as e:JOB['message']=str(e)
    finally:
        JOB['running']=False
        JOB['completed']=JOB.get('completed',0)+1

def snapshot():
    with db() as c:
        sites=[dict(r) for r in c.execute('SELECT * FROM sites ORDER BY id')]
        for s in sites:
            s['runs']=[dict(r) for r in c.execute('SELECT * FROM runs WHERE site_id=? ORDER BY id DESC LIMIT 12',(s['id'],))]
            latest = c.execute("SELECT * FROM runs WHERE site_id=? AND mode='gsc' ORDER BY id DESC LIMIT 1", (s['id'],)).fetchone()
            s['index_summary'] = run_report(c, latest) if latest else None
    return {'sites':sites,'job':JOB.copy(),'connected':(DATA/'token.json').exists(),'configured':(ROOT/'credentials.json').exists()}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass  # OAuth codes must never appear in logs.
    def trusted(self):return self.headers.get('Host') in (f'127.0.0.1:{PORT}',f'localhost:{PORT}')
    def send(self,status,value,kind='application/json'):
        raw=(json.dumps(value,ensure_ascii=False) if kind=='application/json' else value).encode()
        self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer');self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if not self.trusted():return self.send(403,{'error':'Invalid Host'})
        p=urllib.parse.urlsplit(self.path);q=urllib.parse.parse_qs(p.query)
        try:
            if p.path=='/':return self.send(200,(ROOT/'web/index.html').read_text().replace('__SESSION__',SESSION),'text/html')
            if p.path=='/oauth/callback':
                state=q.get('state',[''])[0]
                with LOCK:pending=OAUTH.pop(state,None)
                if not pending or time.time()-pending['at']>600:raise ValueError('授权请求失效，请返回工具重新连接')
                if 'error' in q:raise ValueError('Google 授权未完成')
                cr=credentials();v=token_request({'code':q.get('code',[''])[0],'client_id':cr['client_id'],'client_secret':cr.get('client_secret',''),'redirect_uri':f'http://127.0.0.1:{PORT}/oauth/callback','grant_type':'authorization_code','code_verifier':pending['verifier']})
                v['expires_at']=time.time()+v['expires_in'];save_token(v)
                return self.send(200,'<meta charset="utf-8"><p>Google 已连接。关闭此页，返回 SiteRoll 刷新并导入网站。</p>','text/html')
            if self.headers.get('X-SiteRoll-Token')!=SESSION:return self.send(403,{'error':'请从本地首页访问'})
            if p.path=='/api/state':return self.send(200,snapshot())
            if p.path=='/api/report':
                with db() as c:
                    run=c.execute('SELECT * FROM runs WHERE id=?',(int(q['run'][0]),)).fetchone()
                    if not run:raise ValueError('检查记录不存在')
                    report=run_report(c,run)
                return self.send(200,report)
            if p.path=='/api/results':
                with db() as c:rows=[{'url':r['url'],**json.loads(r['data'])} for r in c.execute('SELECT * FROM results WHERE run_id=?',(int(q['run'][0]),))]
                return self.send(200,rows)
            if p.path=='/api/properties':return self.send(200,google_api('https://www.googleapis.com/webmasters/v3/sites').get('siteEntry',[]))
            self.send(404,{'error':'Not found'})
        except Exception as e:self.send(400,{'error':str(e)})
    def do_POST(self):
        if not self.trusted() or self.headers.get('X-SiteRoll-Token')!=SESSION:return self.send(403,{'error':'Forbidden'})
        origin=self.headers.get('Origin')
        if origin not in (None,f'http://127.0.0.1:{PORT}',f'http://localhost:{PORT}'):return self.send(403,{'error':'Invalid Origin'})
        try:
            size=int(self.headers.get('Content-Length',0))
            if size>100_000:raise ValueError('请求过大')
            v=json.loads(self.rfile.read(size) or b'{}')
            if self.path=='/api/sites':
                url=validate_url(v['url'].strip()).rstrip('/')+'/'
                sitemap=validate_url(v.get('sitemap','').strip() or urllib.parse.urljoin(url,'sitemap.xml'))
                if urllib.parse.urlsplit(url).netloc!=urllib.parse.urlsplit(sitemap).netloc:raise ValueError('站点地图必须位于同域名')
                prop=v.get('property','')
                if prop and not (prop.startswith('sc-domain:') or prop.startswith('https://') or prop.startswith('http://')):raise ValueError('GSC 属性格式错误')
                with db() as c:c.execute('INSERT INTO sites(url,sitemap,property) VALUES(?,?,?) ON CONFLICT(url) DO UPDATE SET sitemap=excluded.sitemap,property=excluded.property',(url,sitemap,prop))
                return self.send(200,{'ok':True})
            if self.path=='/api/delete':
                if JOB['running']:raise ValueError('检查期间不能删除网站')
                with db() as c:
                    c.execute('DELETE FROM results WHERE run_id IN(SELECT id FROM runs WHERE site_id=?)',(v['id'],));c.execute('DELETE FROM runs WHERE site_id=?',(v['id'],));c.execute('DELETE FROM sites WHERE id=?',(v['id'],))
                return self.send(200,{'ok':True})
            if self.path=='/api/scan':
                with LOCK:
                    if JOB['running']:raise ValueError('已有检查正在运行')
                    if v.get('mode') not in ('technical','gsc'):raise ValueError('检查模式错误')
                    if not v.get('ids'):raise ValueError('请先添加网站')
                    JOB.update(running=True,done=0,total=0,message='正在读取 sitemap',run_ids=[])
                    threading.Thread(target=scan,args=(v['ids'],v['mode']),daemon=True).start()
                return self.send(200,{'ok':True})
            if self.path=='/api/connect':
                cr=credentials();state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64)
                with LOCK:OAUTH[state]={'verifier':verifier,'at':time.time()}
                params={'client_id':cr['client_id'],'redirect_uri':f'http://127.0.0.1:{PORT}/oauth/callback','response_type':'code','scope':'https://www.googleapis.com/auth/webmasters.readonly','access_type':'offline','prompt':'consent','state':state,'code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),'code_challenge_method':'S256'}
                return self.send(200,{'url':'https://accounts.google.com/o/oauth2/v2/auth?'+urllib.parse.urlencode(params)})
            if self.path=='/api/disconnect':
                if JOB['running']:raise ValueError('请等待检查完成')
                (DATA/'token.json').unlink(missing_ok=True)
                return self.send(200,{'ok':True})
            self.send(404,{'error':'Not found'})
        except Exception as e:self.send(400,{'error':str(e)})

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    print(f'SiteRoll: http://127.0.0.1:{PORT} — Ctrl+C 停止。数据保存在 data/，请勿提交。')
    if not args.no_browser:webbrowser.open(f'http://127.0.0.1:{PORT}')
    try:server.serve_forever()
    except KeyboardInterrupt:server.server_close()
