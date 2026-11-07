"""Synthetic-only offline rehearsal; this is not an export of real user evidence."""
from hashlib import sha256
from html import escape
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.contracts.reports import FreeReport, PaidReport
from app.report import REPORT_CSP, render_report


def export_fixture(destination: Path, scope: Literal['free', 'paid'] = 'free') -> Path:
    """Create a fresh immutable rehearsal directory from a bundled synthetic DTO.

    No database, storage, model adapter or user-supplied media is read. Scope is
    explicit: free exports never package a paid DTO or paid HTML alongside it.
    A reset is another export; prior output and the input fixture stay untouched.
    """
    if scope not in ('free', 'paid'):
        raise ValueError('unsupported scope')
    source = Path(__file__).resolve().parents[2] / 'tests' / 'fixtures' / f'{scope}_report.json'
    raw = source.read_bytes()
    envelope = json.loads(raw)
    if envelope['metadata']['provenance'] != 'synthetic':
        raise ValueError('only bundled synthetic fixtures are supported')
    model = FreeReport if scope == 'free' else PaidReport
    report = model.model_validate(envelope['data'])
    if report.state != 'ready':
        raise ValueError('only ready reports can be rehearsed')
    html = render_report(report, provenance_label=(
        'Synthetic offline rehearsal — no real inference. Images withheld. Checkout requires an online service.'))
    # file:// exports have no response headers. Carry the same strict styles policy
    # in-document and forbid every image/network/form source for this text/SVG copy.
    policy = REPORT_CSP.replace("img-src 'self'", "img-src 'none'").replace("form-action 'self'", "form-action 'none'")
    html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{escape(policy, quote=True)}">', 1)
    output = destination / f'synthetic-{scope}-{uuid4().hex}'
    output.mkdir(parents=True, exist_ok=False)
    (output / 'index.html').write_text(html, encoding='utf-8', newline='\n')
    receipt = {
        'schema_version': 1, 'provenance': 'synthetic', 'scope': scope,
        'source_sha256': sha256(raw).hexdigest(), 'html_sha256': sha256(html.encode()).hexdigest(),
        'model_requests': 0, 'images': 'withheld', 'checkout': 'unavailable_offline',
        'real_evidence_replay': False, 'recorded_demo_video': False,
    }
    (output / 'manifest.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8', newline='\n')
    return output
