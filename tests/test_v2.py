"""Tests for incremental checks, service accounts, sitemap fixes and the weekly report."""
import base64, gzip, json, shutil, subprocess, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import app


def ok(url='https://example.com/a'):
    return {'inspection': {'indexStatusResult': {'verdict': 'PASS'}}}


def bad(**extra):
    return {'inspection': {'indexStatusResult': {'verdict': 'NEUTRAL', 'coverageState': '已抓取 - 尚未编入索引', **extra}}}


class TempDB(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch = patch('app.DB', Path(self.dir.name) / 'test.db')
        self.patch.start()
        app.init_db()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def site(self, url='https://example.com/', prop='sc-domain:example.com'):
        with app.db() as c:
            return c.execute('INSERT INTO sites(url,sitemap,property) VALUES(?,?,?)', (url, url + 'sitemap.xml', prop)).lastrowid

    def run_with(self, site_id, at, results, mode='gsc'):
        with app.db() as c:
            run = c.execute('INSERT INTO runs(site_id,at,mode) VALUES(?,?,?)', (site_id, at, mode)).lastrowid
            for url, data in results.items():
                c.execute('INSERT INTO results VALUES(?,?,?)', (run, url, json.dumps(data)))
        return run


class SitemapTests(unittest.TestCase):
    def test_www_and_non_www_are_same_site(self):
        body = b'<urlset><url><loc>https://www.example.com/a</loc></url><url><loc>https://example.com/b</loc></url><url><loc>https://blog.example.com/c</loc></url></urlset>'
        with patch('app.fetch', return_value=(200, '', {}, body, [])):
            self.assertEqual(app.sitemap_urls('https://example.com/sitemap.xml', 'https://example.com/'), ['https://www.example.com/a', 'https://example.com/b'])

    def test_domain_property_includes_subdomains(self):
        body = b'<urlset><url><loc>https://blog.example.com/c</loc></url><url><loc>https://evil-example.com/x</loc></url></urlset>'
        with patch('app.fetch', return_value=(200, '', {}, body, [])):
            self.assertEqual(app.sitemap_urls('https://example.com/sitemap.xml', 'https://example.com/', prop='sc-domain:example.com'), ['https://blog.example.com/c'])

    def test_gzip_sitemap_and_lastmod(self):
        body = gzip.compress(b'<urlset><url><loc>https://example.com/a</loc><lastmod>2026-10-01</lastmod></url></urlset>')
        lastmods = {}
        with patch('app.fetch', return_value=(200, '', {}, body, [])):
            self.assertEqual(app.sitemap_urls('https://example.com/sitemap.xml.gz', 'https://example.com/', lastmods=lastmods), ['https://example.com/a'])
        self.assertEqual(lastmods, {'https://example.com/a': '2026-10-01'})

    def test_invalid_xml_has_clear_message(self):
        with patch('app.fetch', return_value=(200, '', {}, b'<html>not a sitemap', [])):
            with self.assertRaisesRegex(ValueError, '有效的 XML'): app.sitemap_urls('https://example.com/sitemap.xml', 'https://example.com/')


class PlanTests(unittest.TestCase):
    def test_priority_and_freshness(self):
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        ago = lambda d: (now - timedelta(days=d)).isoformat()
        e = lambda state, d: [{'at': ago(d), 'state': state}]
        hist = {'fresh': e('indexed', 1), 'stale': e('indexed', 30), 'noidx': e('not_indexed', 5), 'noidx_recent': e('not_indexed', 1),
                'failed': e('error', 1), 'updated': e('indexed', 2)}
        urls = ['fresh', 'stale', 'noidx', 'noidx_recent', 'failed', 'updated', 'brand_new']
        selected, deferred = app.plan_gsc(urls, hist, {'updated': ago(1)}, None, False, now)
        self.assertEqual(selected, [('brand_new', 'new'), ('failed', 'retry'), ('noidx', 'not_indexed'), ('updated', 'updated'), ('stale', 'stale')])
        self.assertEqual(deferred, 2)
        selected, deferred = app.plan_gsc(urls, hist, {}, 2, False, now)
        self.assertEqual([u for u, _ in selected], ['brand_new', 'failed'])
        self.assertEqual(deferred, 5)
        selected, _ = app.plan_gsc(urls, hist, {}, None, True, now)
        self.assertEqual(len(selected), 7)


class ScanTests(TempDB):
    def test_single_url_error_does_not_stop_site(self):
        sid = self.site()
        calls = iter([app.GoogleError('Google API HTTP 400', 400), {'indexStatusResult': {'verdict': 'PASS'}}])
        def fake(url, prop):
            v = next(calls)
            if isinstance(v, Exception): raise v
            return v
        with patch('app.sitemap_urls', return_value=['https://example.com/a', 'https://example.com/b']), patch('app.inspect_url', side_effect=fake), patch('app.time.sleep'):
            app.JOB.update(running=True, done=0, total=0); app.scan([sid], 'gsc')
        with app.db() as c:
            run = c.execute('SELECT * FROM runs').fetchone()
            self.assertEqual(run['error'], '')
            self.assertEqual(run['planned'], 2)
            self.assertEqual(c.execute('SELECT count(*) FROM results').fetchone()[0], 2)

    def test_permission_error_stops_site(self):
        sid = self.site()
        with patch('app.sitemap_urls', return_value=['https://example.com/a', 'https://example.com/b']), patch('app.inspect_url', side_effect=app.GoogleError('HTTP 403', 403)):
            app.scan([sid], 'gsc')
        with app.db() as c:
            self.assertIn('403', c.execute('SELECT error FROM runs').fetchone()[0])
            self.assertEqual(c.execute('SELECT count(*) FROM results').fetchone()[0], 1)

    def test_consecutive_errors_stop_site(self):
        sid = self.site()
        urls = [f'https://example.com/{i}' for i in range(10)]
        with patch('app.sitemap_urls', return_value=urls), patch('app.inspect_url', side_effect=app.GoogleError('HTTP 500', 500)):
            app.scan([sid], 'gsc')
        with app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM results').fetchone()[0], app.MAX_CONSECUTIVE_ERRORS)

    def test_incremental_skips_fresh_and_url_prefix_filters_outside(self):
        sid = self.site('https://example.com/', 'https://example.com/')
        self.run_with(sid, datetime.now(timezone.utc).isoformat(), {'https://example.com/a': ok()})
        urls = ['https://example.com/a', 'https://example.com/b', 'https://www.example.com/c']
        with patch('app.sitemap_urls', return_value=urls), patch('app.inspect_url', return_value={'indexStatusResult': {'verdict': 'PASS'}}) as inspect, patch('app.time.sleep'):
            app.scan([sid], 'gsc')
        self.assertEqual([c.args[0] for c in inspect.call_args_list], ['https://example.com/b'])
        with app.db() as c:
            run = c.execute('SELECT * FROM runs ORDER BY id DESC').fetchone()
            self.assertEqual((run['discovered'], run['planned'], run['deferred'], run['outside']), (3, 1, 1, 1))
            self.assertEqual(c.execute('SELECT count(*) FROM pages').fetchone()[0], 3)

    def test_incremental_report_compares_with_older_runs(self):
        sid = self.site()
        self.run_with(sid, '2026-10-01T00:00:00+00:00', {'a': ok(), 'b': ok()})
        self.run_with(sid, '2026-10-10T00:00:00+00:00', {'b': ok()})
        run = self.run_with(sid, '2026-10-20T00:00:00+00:00', {'a': bad(), 'c': ok()})
        with app.db() as c:
            r = app.run_report(c, c.execute('SELECT * FROM runs WHERE id=?', (run,)).fetchone())
        self.assertEqual({x['url']: x['kind'] for x in r['changes']}, {'a': 'lost_index', 'c': 'first_checked'})


class ServiceAccountTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('openssl'), 'openssl not installed')
    def test_rs256_signature_verifies_with_openssl(self):
        with tempfile.TemporaryDirectory() as d:
            key, pub, msg, sig = (Path(d) / n for n in ('key.pem', 'pub.pem', 'msg', 'sig'))
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(key)], check=True, capture_output=True)
            subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(pub)], check=True, capture_output=True)
            msg.write_bytes(b'header.claims')
            sig.write_bytes(app.rsa_sha256_sign(key.read_text(), b'header.claims'))
            out = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(pub), '-signature', str(sig), str(msg)], capture_output=True, text=True)
            self.assertIn('Verified OK', out.stdout)

    def test_service_account_token_request(self):
        with tempfile.TemporaryDirectory() as d:
            if not shutil.which('openssl'): self.skipTest('openssl not installed')
            key = Path(d) / 'key.pem'
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(key)], check=True, capture_output=True)
            sa = {'type': 'service_account', 'client_email': 'bot@p.iam.gserviceaccount.com', 'private_key': key.read_text()}
            app.SA_TOKEN.clear()
            with patch.dict('os.environ', {'SITEROLL_SERVICE_ACCOUNT': json.dumps(sa)}), patch('app.token_request', return_value={'access_token': 'tok', 'expires_in': 3600}) as req:
                self.assertEqual(app.auth_mode(), 'service_account')
                self.assertEqual(app.access_token(), 'tok')
                self.assertEqual(app.access_token(), 'tok')
            self.assertEqual(req.call_count, 1)
            assertion = req.call_args.args[0]['assertion']
            claims = json.loads(base64.urlsafe_b64decode(assertion.split('.')[1] + '=='))
            self.assertEqual((claims['iss'], claims['scope'], claims['aud']), (sa['client_email'], app.SCOPE, app.TOKEN_URL))


class RetryTests(TempDB):
    def test_retries_transient_then_succeeds(self):
        responses = [app.GoogleError('HTTP 503', 503), app.GoogleError('HTTP 429', 429), {'inspectionResult': {'ok': 1}}]
        def fake(*a, **k):
            v = responses.pop(0)
            if isinstance(v, Exception): raise v
            return v
        with patch('app.google_api', side_effect=fake), patch('app.time.sleep') as sleep:
            self.assertEqual(app.inspect_url('https://example.com/a', 'sc-domain:example.com'), {'ok': 1})
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(app.quota_used('sc-domain:example.com'), 3)

    def test_does_not_retry_permission_error(self):
        with patch('app.google_api', side_effect=app.GoogleError('HTTP 403', 403)) as api, patch('app.time.sleep'):
            with self.assertRaises(app.GoogleError): app.inspect_url('https://example.com/a', 'sc-domain:example.com')
        self.assertEqual(api.call_count, 1)


class ClassifyTests(unittest.TestCase):
    def test_categories(self):
        u = 'https://example.com/a'
        self.assertEqual(app.classify(u, ok())[0], 'indexed')
        self.assertEqual(app.classify(u, bad(robotsTxtState='DISALLOWED'))[0], 'likely_error')
        self.assertEqual(app.classify(u, bad(indexingState='BLOCKED_BY_META_TAG'))[1], 'sitemap 中的页面设置了 noindex')
        self.assertEqual(app.classify(u, bad(pageFetchState='NOT_FOUND'))[1], '404 未找到')
        self.assertEqual(app.classify(u, bad(userCanonical=u, googleCanonical='https://example.com/b'))[0], 'review')
        self.assertEqual(app.classify(u, bad(userCanonical='https://example.com/b', googleCanonical='https://example.com/b'))[0], 'expected')
        self.assertEqual(app.classify(u, bad(pageFetchState='SUCCESSFUL')), ('watch', '已抓取 - 尚未编入索引'))
        self.assertEqual(app.classify(u, {'error': 'x'})[0], 'failed')
        self.assertEqual(app.classify(u, {'inspection': {}})[0], 'unknown')


class WeeklyTests(TempDB):
    def test_weekly_report(self):
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        old, recent = (now - timedelta(days=10)).isoformat(), (now - timedelta(days=1)).isoformat()
        sid = self.site()
        quiet = self.site('https://quiet.com/', 'sc-domain:quiet.com')
        self.run_with(sid, old, {'https://example.com/a': ok(), 'https://example.com/b': bad()})
        self.run_with(sid, recent, {'https://example.com/a': bad(pageFetchState='NOT_FOUND'), 'https://example.com/b': ok()})
        self.run_with(quiet, recent, {'https://quiet.com/x': ok()})
        app.record_pages(sid, ['https://example.com/a', 'https://example.com/b', 'https://example.com/new'], {}, recent)
        app.record_pages(quiet, ['https://quiet.com/x'], {}, recent)
        r = app.weekly_report(7, now)
        self.assertEqual(r['summary']['sites_need_attention'], 1)
        site = r['sites'][0]
        self.assertEqual(site['url'], 'https://example.com/')
        self.assertEqual(site['changes']['lost_index']['urls'], ['https://example.com/a'])
        self.assertEqual(site['changes']['newly_indexed']['urls'], ['https://example.com/b'])
        self.assertEqual(site['pending_first_check']['urls'], ['https://example.com/new'])
        self.assertEqual(site['not_indexed_by_reason']['likely_error']['404 未找到']['count'], 1)
        self.assertFalse(r['sites'][1]['needs_attention'])
        md = app.weekly_markdown(r)
        self.assertIn('需要关注 1 个', md)
        self.assertIn('404 未找到', md)

    def test_write_weekly_files(self):
        self.site()
        with tempfile.TemporaryDirectory() as out:
            _, md, paths = app.write_weekly(7, out)
            self.assertTrue(all(p.exists() for p in paths))
            self.assertIn('本周没有新的 Google 收录数据', md)
            json.loads((Path(out) / 'latest.json').read_text(encoding='utf-8'))


class CliTests(TempDB):
    def test_add_site_and_run_technical_writes_report(self):
        with tempfile.TemporaryDirectory() as out, patch('app.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]):
            self.assertEqual(app.main(['add-site', 'https://www.example.com', '--sitemap', 'https://example.com/sitemap.xml']), 0)
            with patch('app.sitemap_urls', return_value=['https://example.com/a']), patch('app.technical', return_value={'http': 200, 'final': 'https://example.com/a'}):
                self.assertEqual(app.main(['run', '--mode', 'all', '--out', out]), 0)
            self.assertTrue((Path(out) / 'latest.md').exists())
        with app.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM runs WHERE mode='technical'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM runs WHERE mode='gsc'").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
