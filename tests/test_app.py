import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import app

class SiteRollTests(unittest.TestCase):
    def test_private_targets_blocked(self):
        for target in ['http://127.0.0.1/','http://[::1]/','file:///etc/passwd','http://user:pass@example.com/']:
            with self.assertRaises(ValueError):app.validate_url(target)
    def test_public_target(self):
        with patch('app.socket.getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',443))]):
            self.assertEqual(app.validate_url('https://example.com/'),'https://example.com/')
    def test_redirect_target_revalidated(self):
        class Response:
            code=301;headers={'Location':'http://127.0.0.1/'}
            def close(self):pass
        with patch('app.socket.getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',443))]),patch.object(app.PUBLIC_OPENER,'open',return_value=Response()):
            # The second validation must inspect the redirected target, too.
            with patch('app.validate_url',side_effect=[None,ValueError('private')]) as validate:
                with self.assertRaises(ValueError):app.fetch('https://example.com/')
                self.assertEqual(validate.call_args.args[0],'http://127.0.0.1/')
    def test_robots_header_and_relative_canonical(self):
        fake=(200,'https://example.com/page',{'X-Robots-Tag':'noindex'},b'<title>Test</title><link rel="canonical" href="/preferred">',[])
        with patch('app.fetch',return_value=fake):
            r=app.technical('https://example.com/page');self.assertTrue(r['noindex']);self.assertEqual(r['canonical'],'https://example.com/preferred')
    def test_googlebot_none(self):
        with patch('app.fetch',return_value=(200,'https://example.com/',{},b'<meta name="googlebot" content="none">',[])):
            self.assertTrue(app.technical('https://example.com/')['noindex'])
    def test_sitemap_index_and_dedupe(self):
        index=b'<sitemapindex><sitemap><loc>https://example.com/sub.xml</loc></sitemap></sitemapindex>'
        pages=b'<urlset><url><loc>https://example.com/a</loc></url><url><loc>https://example.com/a</loc></url><url><loc>https://other.com/x</loc></url></urlset>'
        with patch('app.fetch',side_effect=[(200,'',{},index,[]),(200,'',{},pages,[])]):
            self.assertEqual(app.sitemap_urls('https://example.com/sitemap.xml','https://example.com/'),['https://example.com/a'])
    def test_sitemap_limit_explicit(self):
        body=b'<urlset><url><loc>https://example.com/a</loc></url><url><loc>https://example.com/b</loc></url></urlset>'
        with patch('app.fetch',return_value=(200,'',{},body,[])):
            with self.assertRaisesRegex(ValueError,'上限'):app.sitemap_urls('https://example.com/sitemap.xml','https://example.com/',1)
    def test_sitemap_entities_rejected(self):
        with patch('app.fetch',return_value=(200,'',{},b'<!DOCTYPE x><urlset/>',[])):
            with self.assertRaisesRegex(ValueError,'实体'):app.sitemap_urls('https://example.com/sitemap.xml','https://example.com/')
    def test_scan_failure_not_mislabeled_and_saved(self):
        with tempfile.TemporaryDirectory() as d,patch('app.DB',Path(d)/'test.db'):
            with app.db() as c:
                c.executescript('CREATE TABLE sites(id INTEGER PRIMARY KEY,url TEXT,sitemap TEXT,property TEXT);CREATE TABLE runs(id INTEGER PRIMARY KEY,site_id INTEGER,at TEXT,mode TEXT,error TEXT DEFAULT "",total INTEGER DEFAULT 0,discovered INTEGER);CREATE TABLE results(run_id INTEGER,url TEXT,data TEXT);')
                c.execute('INSERT INTO sites VALUES(1,?,?,?)',('https://example.com/','https://example.com/sitemap.xml','sc-domain:example.com'))
            with patch('app.sitemap_urls',return_value=['https://example.com/a','https://example.com/b']),patch('app.inspect_url',side_effect=ValueError('Google API HTTP 403')):
                app.JOB.update(running=True,done=0,total=0);app.scan([1],'gsc')
            with app.db() as c:
                r=json.loads(c.execute('SELECT data FROM results').fetchone()[0]);self.assertIn('error',r);self.assertNotIn('inspection',r)
                self.assertIn('403',c.execute('SELECT error FROM runs').fetchone()[0]);self.assertEqual(c.execute('SELECT count(*) FROM results').fetchone()[0],1)
            self.assertFalse(app.JOB['running'])

if __name__=='__main__':unittest.main()

class ReportTests(unittest.TestCase):
    def test_partial_run_does_not_report_missing_or_errors_as_lost_index(self):
        with tempfile.TemporaryDirectory() as d,patch('app.DB',Path(d)/'test.db'):
            with app.db() as c:
                c.executescript('CREATE TABLE runs(id INTEGER PRIMARY KEY,site_id INTEGER,at TEXT,mode TEXT,error TEXT,total INTEGER,discovered INTEGER);CREATE TABLE results(run_id INTEGER,url TEXT,data TEXT);')
                c.executemany('INSERT INTO runs VALUES(?,?,?,?,?,?,?)',[(1,1,'2026-01-01','gsc','',4,4),(2,1,'2026-01-02','gsc','interrupted',0,5)])
                def put(run,url,verdict=None,error=None):
                    data={'error':error} if error else {'inspection':{'indexStatusResult':{'verdict':verdict}}}
                    c.execute('INSERT INTO results VALUES(?,?,?)',(run,url,json.dumps(data)))
                for url in ('a','b','c'):put(1,url,'PASS')
                put(1,'d','FAIL');put(2,'a','FAIL');put(2,'b',error='403');put(2,'d','PASS');put(2,'e','PASS')
                r=app.run_report(c,c.execute('SELECT * FROM runs WHERE id=2').fetchone())
                self.assertEqual(r['unchecked'],1);self.assertEqual(r['counts']['failed'],1);self.assertEqual(r['counts']['indexed'],2)
                self.assertEqual({x['url']:x['kind'] for x in r['changes']},{'a':'lost_index','d':'newly_indexed','e':'first_checked'})
    def test_first_run_has_no_changes_and_unknown_is_not_unindexed(self):
        with tempfile.TemporaryDirectory() as d,patch('app.DB',Path(d)/'test.db'):
            with app.db() as c:
                c.executescript('CREATE TABLE runs(id INTEGER PRIMARY KEY,site_id INTEGER,at TEXT,mode TEXT,error TEXT,total INTEGER,discovered INTEGER);CREATE TABLE results(run_id INTEGER,url TEXT,data TEXT);')
                c.execute('INSERT INTO runs VALUES(1,1,"today","gsc","",1,NULL)');c.execute('INSERT INTO results VALUES(1,"a",?)',(json.dumps({'inspection':{}}),))
                r=app.run_report(c,c.execute('SELECT * FROM runs').fetchone());self.assertIsNone(r['unchecked']);self.assertEqual(r['counts']['unknown'],1);self.assertEqual(r['counts']['not_indexed'],0);self.assertEqual(r['changes'],[])
    def test_technical_issue_changes_and_mode_separation(self):
        with tempfile.TemporaryDirectory() as d,patch('app.DB',Path(d)/'test.db'):
            with app.db() as c:
                c.executescript('CREATE TABLE runs(id INTEGER PRIMARY KEY,site_id INTEGER,at TEXT,mode TEXT,error TEXT,total INTEGER,discovered INTEGER);CREATE TABLE results(run_id INTEGER,url TEXT,data TEXT);')
                c.executemany('INSERT INTO runs VALUES(?,?,?, ?,"",2,2)',[(1,1,'one','technical'),(2,1,'two','gsc'),(3,1,'three','technical')])
                for run,url,data in [(1,'a',{'http':200}),(1,'b',{'noindex':True}),(3,'a',{'noindex':True}),(3,'b',{'http':200})]:c.execute('INSERT INTO results VALUES(?,?,?)',(run,url,json.dumps(data)))
                r=app.run_report(c,c.execute('SELECT * FROM runs WHERE id=3').fetchone());self.assertEqual(r['baseline']['id'],1);self.assertEqual([x['kind'] for x in r['changes']],['new_issue','resolved_issue'])
