from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import socket

import pytest

from app.report.offline import export_fixture


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.active = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'iframe', 'form', 'object', 'embed', 'img', 'link', 'video'):
            self.active.append(tag)
        self.urls.extend(value for key, value in attrs if key in ('src', 'srcset', 'href', 'action'))


@pytest.mark.parametrize('scope', ['free', 'paid'])
def test_export_rehearses_without_network_and_preserves_previous_output(tmp_path, monkeypatch, scope):
    def forbidden(*args, **kwargs):
        pytest.fail('offline export attempted network access')
    monkeypatch.setattr(socket, 'socket', forbidden)
    first = export_fixture(tmp_path, scope)
    original = (first / 'index.html').read_bytes()
    second = export_fixture(tmp_path, scope)
    assert first != second and (first / 'index.html').read_bytes() == original
    assert (second / 'index.html').read_bytes() == original
    assert {p.name for p in first.iterdir()} == {'index.html', 'manifest.json'}
    parsed = References()
    parsed.feed(original.decode())
    assert all(url.startswith('#') for url in parsed.urls) and not parsed.active
    assert 'Synthetic offline rehearsal' in original.decode()
    assert 'Content-Security-Policy' in original.decode()
    receipt = json.loads((first / 'manifest.json').read_text())
    assert receipt['html_sha256'] == sha256(original).hexdigest()
    assert receipt['model_requests'] == 0 and receipt['real_evidence_replay'] is False


def test_free_package_excludes_locked_ids_and_paid_data(tmp_path):
    root = Path(__file__).parent / 'fixtures'
    paid = json.loads((root / 'paid_report.json').read_text())['data']
    free = json.loads((root / 'free_report.json').read_text())['data']
    free_ids = {row['id'] for row in free['findings']}
    path = export_fixture(tmp_path)
    content = ''.join(p.read_text(encoding='utf-8') for p in path.iterdir())
    for row in paid['findings']:
        if row['id'] not in free_ids:
            assert row['id'] not in content
    assert 'Repair estimates' not in content and 'Listing claims' not in content
