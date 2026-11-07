"""Synthetic-only privacy gate tests; integration uses real PostgreSQL and storage emulator."""
from pathlib import Path
from uuid import uuid4
import pytest
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.contracts.evidence import BoundingBox
from app.models import Artifact, PrivacyDerivative, Report, Session
from app.pipeline.derivatives import render_derivative
from app.services.privacy import PrivacyService, RECEIPT, operator_auth
from app.services.problems import ProblemError
from app.settings import Settings
from tests.test_ingest import media_dir, scratch_root  # noqa: F401
from tests.test_orchestration import isolate, rows  # noqa: F401
from tests.test_report_stages import build
from tests.test_uploads import bucket, make, session_factory  # noqa: F401


def test_solid_redaction_and_metadata_removal(tmp_path):
    source, target = tmp_path/'source.png', tmp_path/'target.png'
    Image.new('RGB', (100, 80), 'white').save(source)
    render_derivative(source, target, width=100, height=80,
        bbox=BoundingBox(x=.1,y=.1,w=.3,h=.3), redactions=(BoundingBox(x=0,y=0,w=.5,h=.5),))
    with Image.open(target) as image:
        assert image.getpixel((10,10)) == (0,0,0)
        assert not image.getexif()
        assert image.size == (100,80)


@pytest.mark.parametrize('kind', ['invalid','geometry','pixels'])
def test_renderer_failure_never_copies_source(tmp_path, kind, monkeypatch):
    source, target = tmp_path/'source', tmp_path/'target'
    if kind == 'invalid':
        source.write_bytes(b'not an image')
    else:
        Image.new('RGB', (20,20), 'white').save(source, format='PNG')
    if kind == 'pixels':
        monkeypatch.setattr('app.pipeline.derivatives.MAX_PIXELS',399)
    with pytest.raises((OSError, ValueError)):
        render_derivative(source, target, width=20 if kind == 'pixels' else 21, height=20,
            bbox=BoundingBox(x=0,y=0,w=1,h=1), redactions=())
    assert not target.exists()


def test_operator_is_separate_disabled_and_constant_time_auth():
    for settings, token in [(Settings(), 'anything'), (Settings(operator_token='operator'), 'owner'),
                            (Settings(operator_token='operator'), None)]:
        with pytest.raises(ProblemError):
            operator_auth(settings, token)
    operator_auth(Settings(operator_token='operator'), 'operator')


@pytest.fixture
def ready(make, media_dir, scratch_root):
    return build(lambda **kwargs: make(operator_token="test-operator", **kwargs), media_dir, scratch_root, parts=(1,2))


def candidate(rig, part_no=1):
    service = PrivacyService(rig.h.db, rig.h.storage)
    finding = next(f for f in rig.snapshot().manifest.all_findings if f.part_no == part_no and f.status == 'verified')
    body = service.create(rig.h.session_id, rig.revision().id, finding.id,
        (BoundingBox(x=0,y=0,w=.1,h=.1),))
    return service, finding, body


def test_exact_manual_receipt_and_current_scoped_derivative_only(ready):
    from app.services.reports import ReportService
    service, finding, body = candidate(ready)
    report = rows(ready.h, Report)[0]
    reports = ReportService(ready.h.db, ready.h.settings, ready.h.storage)
    assert not next(f for f in reports.get(report.share_slug, ready.h.headers['Authorization']).dto.findings if f.id == finding.id).evidence_available
    for digest, receipt in [('0'*64,RECEIPT),(body['sha256'],'approved')]:
        with pytest.raises(ProblemError):
            service.approve(body['id'], digest, receipt)
    service.approve(body['id'], body['sha256'], RECEIPT)
    service.approve(body['id'], body['sha256'], RECEIPT)  # retry is idempotent
    dto = reports.get(report.share_slug, ready.h.headers['Authorization']).dto
    assert next(f for f in dto.findings if f.id == finding.id).evidence_available
    url = reports.frame_url(report.share_slug, finding.id, ready.h.headers['Authorization'])
    assert 'X-Amz-Expires=60' in url and '/derivatives/' in url
    assert service.preview(body['id']).startswith(b'\x89PNG')
    # Part 2 approval never grants free scope evidence or even finding membership.
    _, paid_finding, paid_body = candidate(ready, 2)
    service.approve(paid_body['id'], paid_body['sha256'], RECEIPT)
    with pytest.raises(ProblemError):
        reports.frame_url(report.share_slug, paid_finding.id, ready.h.headers['Authorization'])
    # Global public sharing gate remains closed even after manual review.
    with pytest.raises(ProblemError):
        reports.frame_url(report.share_slug, finding.id)


def test_overwritten_derivative_and_deleted_session_withhold(ready, tmp_path):
    from app.services.reports import ReportService
    service, finding, body = candidate(ready)
    service.approve(body['id'], body['sha256'], RECEIPT)
    with ready.h.db() as db:
        key = db.get(PrivacyDerivative, body['id']).object_key
    replacement = tmp_path/'changed.png'
    Image.new('RGB',(20,20),'red').save(replacement)
    ready.h.storage.put_file(key,replacement,'image/png')
    report = rows(ready.h,Report)[0]
    reports = ReportService(ready.h.db,ready.h.settings,ready.h.storage)
    with pytest.raises(ProblemError):
        reports.frame_url(report.share_slug,finding.id,ready.h.headers['Authorization'])
    with ready.h.db.begin() as db:
        db.execute(update(Session).where(Session.id == ready.h.session_id).values(state='deleting'))
    with pytest.raises(ProblemError):
        service.approve(body['id'],body['sha256'],RECEIPT)


def test_binding_cannot_be_mutated_after_creation(ready):
    service, finding, body = candidate(ready)
    with pytest.raises(IntegrityError):
        with ready.h.db.begin() as db:
            db.execute(update(PrivacyDerivative).where(PrivacyDerivative.id == body['id']).values(sha256='f'*64))
    service.approve(body['id'],body['sha256'],RECEIPT)
    with pytest.raises(IntegrityError):
        with ready.h.db.begin() as db:
            db.execute(update(PrivacyDerivative).where(PrivacyDerivative.id == body['id']).values(review_receipt=None))

def test_annotation_failure_and_source_hash_mismatch_withhold(ready, monkeypatch):
    from app.services import privacy
    finding = next(f for f in ready.snapshot().manifest.all_findings if f.part_no == 1 and f.status == 'verified')
    service = PrivacyService(ready.h.db,ready.h.storage)
    def broken(*args,**kwargs):
        raise ValueError('synthetic annotation failure')
    monkeypatch.setattr(privacy,'render_derivative',broken)
    with pytest.raises(ProblemError):
        service.create(ready.h.session_id,ready.revision().id,finding.id,())
    assert rows(ready.h,PrivacyDerivative) == []
    monkeypatch.undo()
    with ready.h.db.begin() as db:
        db.execute(update(Artifact).where(Artifact.session_id == ready.h.session_id,
            Artifact.key == finding.evidence.frame_key).values(sha256='f'*64))
    with pytest.raises(ProblemError):
        service.create(ready.h.session_id,ready.revision().id,finding.id,())
    assert rows(ready.h,PrivacyDerivative) == []


def test_deletion_between_hash_read_and_signing_revokes(ready, monkeypatch):
    from app.services.reports import ReportService
    service,finding,body = candidate(ready)
    service.approve(body['id'],body['sha256'],RECEIPT)
    original = ready.h.storage.download_to
    def deleting_download(*args,**kwargs):
        result = original(*args,**kwargs)
        with ready.h.db.begin() as db:
            db.execute(update(Session).where(Session.id == ready.h.session_id).values(state='deleting'))
        return result
    monkeypatch.setattr(ready.h.storage,'download_to',deleting_download)
    reports = ReportService(ready.h.db,ready.h.settings,ready.h.storage)
    with pytest.raises(ProblemError):
        reports.frame_url(rows(ready.h,Report)[0].share_slug,finding.id,ready.h.headers['Authorization'])


def test_operator_http_candidate_preview_and_hash_approval(ready):
    h = ready.h
    finding = next(f for f in ready.snapshot().manifest.all_findings if f.part_no == 1 and f.status == 'verified')
    payload = {'session_id': str(h.session_id), 'revision_id': str(ready.revision().id),
               'finding_id': str(finding.id), 'redactions': []}
    base = '/api/v1/operator/privacy/derivatives'
    assert h.http.post(base,headers=h.headers,json=payload).status_code == 404
    operator = {'X-Operator-Token': 'test-operator'}
    assert h.http.post(base,headers=operator,json={**payload,'approved':True}).status_code == 422
    result = h.http.post(base,headers=operator,json=payload)
    assert result.status_code == 201, result.text
    body = result.json()
    assert body['state'] == 'pending_manual_review'
    preview = h.http.get(base+'/'+body['id'],headers=operator)
    assert preview.status_code == 200 and preview.content.startswith(b'\x89PNG')
    assert 'no-store' in preview.headers['cache-control']
    assert h.http.get(base+'/'+body['id'],headers=h.headers).status_code == 404
    approved = h.http.post(base+'/'+body['id']+'/approve',headers=operator,
        json={'sha256':body['sha256'],'manual_review_receipt':RECEIPT})
    assert approved.status_code == 200, approved.text
    report = rows(h,Report)[0]
    frame = h.http.get(f'/api/v1/reports/{report.share_slug}/frames/{finding.id}',headers=h.headers,follow_redirects=False)
    assert frame.status_code == 302 and 'X-Amz-Expires=60' in frame.headers['location']
    assert 'no-store' in frame.headers['cache-control'] and frame.headers['referrer-policy'] == 'no-referrer'

def test_cross_session_and_raw_namespace_bindings_rejected(ready, make):
    service, finding, body = candidate(ready)
    with ready.h.db() as db:
        original = db.get(PrivacyDerivative,body['id'])
        values = {column.name: getattr(original,column.name) for column in PrivacyDerivative.__table__.columns
                  if column.name not in ('id','created_at','object_key','session_id')}
    other = make()
    for sid,key in [(other.session_id,None),(ready.h.session_id,finding.evidence.frame_key)]:
        identity = uuid4()
        with pytest.raises(IntegrityError):
            with ready.h.db.begin() as db:
                db.add(PrivacyDerivative(id=identity,session_id=sid,
                    object_key=key or f'media/{sid}/derivatives/{identity}',**values))
