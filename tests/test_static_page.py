"""The single-file page carries the whole payload and needs nothing from the server."""
import base64
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from back.api import static_page


class StaticPageTest(unittest.TestCase):
    def test_assets_and_data_are_inlined(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / 'dist'
            (dist / 'assets').mkdir(parents=True)
            (dist / 'index.html').write_text('<!doctype html>\n<head><script type="module" crossorigin src="/assets/index-a.js"></script>\n'
                                             '<link rel="stylesheet" crossorigin href="/assets/index-b.css"></head><body></body>')
            (dist / 'assets' / 'index-a.js').write_text('const logo="/assets/logo-c.svg";const s="</script>";')
            (dist / 'assets' / 'index-b.css').write_text('.a{background:url(/assets/logo-c.svg)}')
            (dist / 'assets' / 'logo-c.svg').write_text('<svg/>')
            (dist / 'map.png').write_bytes(b'\x89PNG')
            (dist / 'assets' / 'index-a.js').write_text((dist / 'assets' / 'index-a.js').read_text() + 'const m="/map.png";')
            payload = {'version': 'v1', 'stations': {'1': {'역명': '길동역'}}}
            out = static_page.build(Path(tmp) / 'out' / 'index.html', payload, dist)
            html = out.read_text(encoding='utf-8')
            self.assertNotIn('/assets/', html)
            self.assertIn('const m="data:image/png;base64,', html)
            self.assertIn('<style>.a{background:url(data:image/svg+xml;base64,', html)
            self.assertIn('const s="<\\/script>"', html)
            packed = html.split('window.__STATIC_DATA_GZ__="')[1].split('"')[0]
            self.assertEqual(json.loads(gzip.decompress(base64.b64decode(packed))), payload)
            self.assertEqual(out.stat().st_mode & 0o077, 0)
            with self.assertRaises(FileNotFoundError):
                static_page.build(Path(tmp) / 'out2.html', payload, Path(tmp) / 'missing')
            # The copy holds every office's data: never write it where the server (or the next build) publishes files.
            login_page = (dist / 'index.html').read_text()
            for public in (dist / 'index.html', dist / 'saved' / 'page.html', Path(tmp) / 'public' / 'page.html',
                           Path(tmp) / 'out' / '..' / 'dist' / 'index.html'):
                with self.assertRaises(ValueError):
                    static_page.build(public, payload, dist)
            self.assertEqual((dist / 'index.html').read_text(), login_page)
            # An earlier copy may be replaced; any other existing file is left alone.
            self.assertEqual(static_page.build(out, payload | {'version': 'v2'}, dist), out)
            # ...including one that merely mentions the data variable, like the screen's own source file.
            for name, text in (('notes.html', '<p>회의 자료</p>'),
                               ('api.js', 'const packed = typeof window !== "undefined" ? window.__STATIC_DATA_GZ__ : undefined;'),
                               ('copy_of_static_page.py', Path(static_page.__file__).read_text(encoding='utf-8'))):   # it quotes the opening itself
                other = Path(tmp) / name
                other.write_text(text)
                with self.assertRaises(ValueError):
                    static_page.build(other, payload, dist)
                self.assertEqual(other.read_text(), text)


if __name__ == '__main__':
    unittest.main()
