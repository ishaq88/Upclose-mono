"""Synthetic algorithm and schema checks only; no live dataset, provider or accuracy claim."""
import json
from pathlib import Path
import subprocess
import sys
import pytest
from pydantic import ValidationError
from app.evaluation import DatasetManifest, PredictionBundle, evaluate, input_hash, match_boxes
from app.evaluation.schema import Box

H = 'a'*64


def box(identity='truth',x=.1,y=.1,w=.2,h=.2,concept='rust',component='rocker',severity=None):
    return {'id':identity,'concept':concept,'component_id':component,
        'bbox':{'x':x,'y':y,'w':w,'h':h},'severity':severity}


def image(identity='image',*,car='car',split='heldout',positive=True,part=1):
    return {'id':identity,'car_id':car,'frame_id':'frame-'+identity,'media_sha256':input_hash(identity.encode()),
        'split':split,'part_no':part,'permission':'synthetic','permission_receipt_sha256':H,'reviewer':'synthetic-reviewer',
        'blur':'clear','lighting':'daylight','reviewed_concepts':['rust'],
        'rust_category':'positive' if positive else 'clean','labels':[box()] if positive else []}


def dataset(images=None,walks=None):
    return {'schema_version':'upclose-dataset-v1','provenance':'synthetic',
        'images':images if images is not None else [image()],'walkarounds':walks or []}


def bundle(data, *, omit=(), predictions=None):
    return {'schema_version':'upclose-predictions-v1','dataset_sha256':H,'provenance':'synthetic',
        'provider_model_id':'synthetic-model','prompt_revision':'synthetic-v1','prompt_sha256':H,'config_sha256':H,
        'captured_receipt_sha256':H,'enabled_concepts':['rust'],
        'images':[{'image_id':i['id'],'frame_id':i['frame_id'],'media_sha256':i['media_sha256'],
            'predictions':i['labels'] if predictions is None else predictions} for i in data['images'] if i['id'] not in omit]}


def run(data,predictions=None,split='heldout'):
    return evaluate(DatasetManifest.model_validate(data),PredictionBundle.model_validate(predictions or bundle(data)),
        dataset_sha256=H,predictions_sha256=H,split=split)


def test_duplicate_predictions_are_false_positives_and_matching_is_deterministic():
    truth = (Box.model_validate(box()),)
    guesses = tuple(Box.model_validate(box(identity)) for identity in ('b','a'))
    assert len(match_boxes(truth,guesses)) == 1
    assert match_boxes(truth,guesses) == match_boxes(truth,guesses[::-1])
    data = dataset()
    receipt = run(data,bundle(data,predictions=[box('a'),box('b')]))
    assert receipt['per_concept']['rust']['tp'] == 1
    assert receipt['per_concept']['rust']['fp'] == 1
    assert receipt['per_concept']['rust']['precision'] == .5


def test_maximum_matching_repairs_greedy_trap():
    labels = tuple(Box.model_validate(b) for b in [box('t1',x=0,w=.4),box('t2',x=.2,w=.4)])
    guesses = tuple(Box.model_validate(b) for b in [box('a',x=.1,w=.4),box('b',x=0,w=.35)])
    assert match_boxes(labels,guesses) == (('t1','b'),('t2','a'))


def test_iou_threshold_inclusive_and_component_concept_identity_required():
    truth = Box.model_validate(box(x=0,w=.4))
    exactly = Box.model_validate(box('half',x=0,w=.2))
    assert len(match_boxes((truth,),(exactly,))) == 1
    for guess in [box('tiny',x=0,w=.19),box('different',component='hood'),box('dent',concept='dent')]:
        assert not match_boxes((truth,),(Box.model_validate(guess),))


@pytest.mark.parametrize('field,value',[('frame_id','unrelated-frame'),('media_sha256','b'*64),('image_id','unknown')])
def test_cross_frame_or_changed_media_never_matches(field,value):
    data = dataset()
    predictions = bundle(data)
    predictions['images'][0][field] = value
    with pytest.raises(ValueError,match='evidence identity'):
        run(data,predictions)


def test_missing_predictions_are_explicit_failures_not_empty_clean_assessments():
    data = dataset()
    receipt = run(data,bundle(data,omit=('image',)))
    assert receipt['missing_prediction_images'] == ['image']
    assert receipt['per_image'][0]['status'] == 'missing_predictions'
    assert receipt['per_concept']['rust']['fn'] == 1 and receipt['per_concept']['rust']['recall'] == 0
    assert receipt['gates']['offline_rust_smoke'] == 'not_evaluated'


def test_zero_denominators_and_absent_positive_class_are_not_evaluable():
    data = dataset([image(positive=False)])
    metric = run(data)['per_concept']['rust']
    assert metric['precision'] is None and metric['recall'] is None and not metric['evaluable']
    metric = run(data,bundle(data,predictions=[box('false-positive')]))['per_concept']['rust']
    assert metric['precision'] == 0 and metric['recall'] is None and not metric['evaluable']


def test_tuning_car_leakage_and_duplicate_bytes_are_rejected():
    for images in [[image('a',split='tuning'),image('b',split='heldout')],
                   [image('a'),{**image('b',car='other'),'media_sha256':input_hash(b'a')}]]:
        with pytest.raises(ValidationError):
            DatasetManifest.model_validate(dataset(images))


def test_permission_review_and_schema_are_required_not_client_flags():
    for field in ('permission','permission_receipt_sha256','reviewer','blur','lighting','labels'):
        data = dataset()
        del data['images'][0][field]
        with pytest.raises(ValidationError):
            DatasetManifest.model_validate(data)
    data = dataset()
    data['live_verified'] = True
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate(data)
    data = dataset()
    data['provenance'] = 'live'
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate(data)  # synthetic permission cannot be relabeled live


def test_rust_smoke_minimum_denominators_and_provisional_recall_gate():
    images = [image(str(i),car='car-'+str(i),positive=i<10) for i in range(20)]
    data = dataset(images)
    receipt = run(data)
    assert receipt['gates']['offline_rust_smoke'] == 'passed_offline'
    assert receipt['counts']['rust_positive_images'] == receipt['counts']['rust_clean_or_confounder_images'] == 10
    predictions = bundle(data)
    for row in predictions['images'][:5]:
        row['predictions'] = []
    assert run(data,predictions)['gates']['offline_rust_smoke'] == 'failed_offline'
    assert run(dataset(images[:-1]))['gates']['offline_rust_smoke'] == 'not_evaluated'


def large_dataset():
    images = [image(str(i),car='car-'+str(i//8) if i<24 else 'car-extra-'+str(i),
                    positive=i<50,part=i%8+1) for i in range(124)]
    walks = [{'id':'walk-'+str(c),'car_id':'car-'+str(c),'split':'heldout','complete':True,
        'image_ids':[str(i) for i in range(c*8,c*8+8)],'reviewer':'synthetic-reviewer',
        'source_video_sha256':input_hash(('video-'+str(c)).encode()),'completeness_receipt_sha256':H} for c in range(3)]
    return dataset(images,walks)


def test_heldout_diagnostic_counts_are_checked_but_live_flag_never_attests_access():
    data = large_dataset()
    assert run(data)['gates']['offline_heldout_quality'] == 'passed_offline'
    assert run(data)['counts']['standalone_images'] == 100
    predictions = bundle(data)
    data['provenance'] = predictions['provenance'] = 'live'
    for row in data['images']:
        row['permission'] = 'consent'
    receipt = run(data,predictions)
    assert receipt['gates']['offline_heldout_quality'] == 'passed_offline'
    assert not receipt['provenance_verified']
    for gate in ('live_quality','live_feasibility','standard_access','provider_prices','latency','spend'):
        assert receipt['gates'][gate] == 'unverified'
    data['walkarounds'] = data['walkarounds'][:2]
    assert run(data,predictions)['gates']['offline_heldout_quality'] == 'not_evaluated'


def test_incomplete_walkaround_and_unreviewed_enabled_concept_are_rejected():
    data = large_dataset()
    data['walkarounds'][0]['image_ids'] = ['0']
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate(data)
    data = dataset()
    predictions = bundle(data)
    predictions['enabled_concepts'].append('dent')
    with pytest.raises(ValueError,match='reviewed'):
        run(data,predictions)


def test_severity_agreement_only_uses_available_matched_labels():
    data = dataset()
    data['images'][0]['labels'][0]['severity'] = 3
    predictions = bundle(data)
    predictions['images'][0]['predictions'] = [box('predicted',severity=3)]
    assert run(data,predictions)['severity']['exact_agreement'] == 1
    predictions['images'][0]['predictions'][0]['severity'] = None
    severity = run(data,predictions)['severity']
    assert severity['exact_agreement'] is None and severity['missing_prediction_severity'] == 1


def test_cli_hashes_inputs_and_sanitizes_invalid_input(tmp_path):
    data = dataset()
    dataset_path,prediction_path,output = (tmp_path/name for name in ('dataset.json','predictions.json','receipt.json'))
    raw = json.dumps(data).encode()
    dataset_path.write_bytes(raw)
    predictions = bundle(data)
    predictions['dataset_sha256'] = input_hash(raw)
    prediction_path.write_text(json.dumps(predictions))
    command = [sys.executable,'scripts/evaluate_predictions.py','--dataset',str(dataset_path),
        '--predictions',str(prediction_path),'--output',str(output)]
    result = subprocess.run(command,capture_output=True,text=True)
    assert result.returncode == 0,result.stderr
    receipt = json.loads(output.read_text())
    assert receipt['input_hashes']['dataset'] == input_hash(raw)
    assert receipt['gates']['live_quality'] == 'unverified'
    output.unlink()
    dataset_path.write_text('{"private_payload":"must-never-appear"}')
    result = subprocess.run(command,capture_output=True,text=True)
    assert result.returncode == 2 and not output.exists()
    assert 'must-never-appear' not in result.stderr and 'private_payload' not in result.stderr


def test_absent_enabled_class_cannot_hide_in_successful_pooled_quality():
    data = large_dataset()
    for row in data['images']:
        row['reviewed_concepts'].append('dent')
    predictions = bundle(data)
    predictions['enabled_concepts'].append('dent')
    receipt = run(data,predictions)
    assert receipt['gates']['offline_heldout_per_concept']['rust'] == 'passed_offline'
    assert receipt['gates']['offline_heldout_per_concept']['dent'] == 'not_evaluated'
    assert receipt['gates']['offline_heldout_quality'] == 'not_evaluated'
    assert 'no_positive_examples:dent' in receipt['failures']


def test_dataset_hash_mismatch_and_duplicate_prediction_identity_rejected():
    data = dataset()
    predictions = bundle(data)
    predictions['dataset_sha256'] = 'b'*64
    with pytest.raises(ValueError,match='dataset hash'):
        run(data,predictions)
    predictions = bundle(data,predictions=[box('duplicate'),box('duplicate')])
    with pytest.raises(ValidationError):
        PredictionBundle.model_validate(predictions)


def test_walkaround_frames_do_not_count_toward_hundred_standalone_images():
    data = large_dataset()
    data['images'] = data['images'][:100]
    receipt = run(data)
    assert receipt['counts']['images'] == 100 and receipt['counts']['standalone_images'] == 76
    assert receipt['gates']['offline_heldout_quality'] == 'not_evaluated'


def test_subnormal_geometry_fails_with_sanitized_value_error():
    from app.evaluation.metrics import iou
    from app.evaluation.schema import Box
    tiny = Box.model_validate(box('tiny') | {'bbox': {'x': 0, 'y': 0, 'w': 1e-200, 'h': 1e-200}})
    with pytest.raises(ValueError, match='numeric precision'):
        iou(tiny, tiny)
