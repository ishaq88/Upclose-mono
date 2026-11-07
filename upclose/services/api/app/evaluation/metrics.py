"""Deterministic maximum-cardinality same-evidence matching and offline-only receipts."""
import hashlib
import json
from typing import Any
from app.contracts.enums import Concept
from .schema import Box, DatasetManifest, PredictionBundle, Split

IOU_THRESHOLD = 0.5
METRIC_CONFIG = {'version':'upclose-offline-evaluation-v1','iou_threshold':IOU_THRESHOLD,
    'matching':'maximum-cardinality-same-image-frame-concept-component',
    'rust_smoke_recall':0.60,'rust_precision':0.85,'rust_recall':0.75,
    'other_precision':0.80,'other_recall':0.65,'heldout_minimum_standalone_images':100,
    'heldout_minimum_complete_walkaround_cars':3}


def input_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iou(left: Box, right: Box) -> float:
    a,b = left.bbox,right.bbox
    width = max(0.0,min(a.x+a.w,b.x+b.w)-max(a.x,b.x))
    height = max(0.0,min(a.y+a.h,b.y+b.h)-max(a.y,b.y))
    intersection = width*height
    union = a.w*a.h+b.w*b.h-intersection
    if union <= 0:
        raise ValueError('geometry area is below numeric precision')
    return intersection/union


def match_boxes(labels: tuple[Box,...], predictions: tuple[Box,...]) -> tuple[tuple[str,str],...]:
    """Maximum cardinality, with stable IDs/IoU ordering; one finding can match only once.

    Caller groups by exact image/frame/hash. Component and concept must agree here.
    Augmenting paths repair a greedy match that would otherwise hide a true positive.
    """
    truth = sorted(labels,key=lambda box:box.id)
    predicted = sorted(predictions,key=lambda box:box.id)
    edges = {p.id: [t.id for t in sorted(truth,key=lambda t:(-iou(t,p),t.id))
        if t.concept == p.concept and t.component_id == p.component_id and iou(t,p) >= IOU_THRESHOLD]
        for p in predicted}
    assignment: dict[str,str] = {}
    def augment(prediction_id: str, visited: set[str]) -> bool:
        for label_id in edges[prediction_id]:
            if label_id in visited:
                continue
            visited.add(label_id)
            if label_id not in assignment or augment(assignment[label_id],visited):
                assignment[label_id] = prediction_id
                return True
        return False
    for prediction in predicted:
        augment(prediction.id,set())
    return tuple(sorted(assignment.items()))


def counts(tp: int, fp: int, fn: int) -> dict[str,Any]:
    return {'tp':tp,'fp':fp,'fn':fn,'ground_truth_positives':tp+fn,'predicted_positives':tp+fp,
        'precision':tp/(tp+fp) if tp+fp else None,'recall':tp/(tp+fn) if tp+fn else None,
        'evaluable':tp+fn > 0}


def evaluate(dataset: DatasetManifest, predictions: PredictionBundle, *, dataset_sha256: str,
             predictions_sha256: str, split: Split = 'heldout') -> dict[str,Any]:
    if predictions.dataset_sha256 != dataset_sha256:
        raise ValueError('prediction dataset hash mismatch')
    selected = [image for image in dataset.images if image.split == split]
    if not selected:
        raise ValueError('requested split has no images')
    all_images = {image.id:image for image in dataset.images}
    captured = {image.image_id:image for image in predictions.images}
    for identity,row in captured.items():
        image = all_images.get(identity)
        if image is None or row.frame_id != image.frame_id or row.media_sha256 != image.media_sha256:
            raise ValueError('prediction evidence identity mismatch')
    enabled = sorted(predictions.enabled_concepts,key=str)
    if any(not set(enabled) <= set(image.reviewed_concepts) for image in selected):
        raise ValueError('selected image lacks labels reviewed for every enabled concept')
    totals = {str(concept):[0,0,0] for concept in enabled}
    per_image: list[dict[str,Any]] = []
    severity = {'matched_labeled':0,'compared':0,'exact':0,'missing_prediction_severity':0}
    missing: list[str] = []
    for image in sorted(selected,key=lambda row:row.id):
        row = captured.get(image.id)
        if row is None:
            missing.append(image.id)
        local: dict[str,Any] = {}
        for concept in enabled:
            labels = tuple(box for box in image.labels if box.concept == concept)
            predicted = tuple(box for box in row.predictions if box.concept == concept) if row else ()
            matched = match_boxes(labels,predicted)
            tp,fp,fn = len(matched),len(predicted)-len(matched),len(labels)-len(matched)
            local[str(concept)] = counts(tp,fp,fn)
            for index,value in enumerate((tp,fp,fn)):
                totals[str(concept)][index] += value
            truths,guesses = {b.id:b for b in labels},{b.id:b for b in predicted}
            for truth_id,prediction_id in matched:
                truth,guess = truths[truth_id],guesses[prediction_id]
                if truth.severity is not None:
                    severity['matched_labeled'] += 1
                    if guess.severity is None:
                        severity['missing_prediction_severity'] += 1
                    else:
                        severity['compared'] += 1
                        severity['exact'] += int(guess.severity == truth.severity)
        per_image.append({'image_id':image.id,'car_id':image.car_id,'frame_id':image.frame_id,
            'status':'missing_predictions' if row is None else 'evaluated',
            'conditions':{'blur':image.blur,'lighting':image.lighting},'concepts':local})
    summary = {concept:counts(*values) for concept,values in totals.items()}
    positive = sum(image.rust_category == 'positive' for image in selected)
    negative = sum(image.rust_category in ('clean','confounder') for image in selected)
    walks = {walk.car_id for walk in dataset.walkarounds if walk.split == split and walk.complete}
    walkaround_images = {identity for walk in dataset.walkarounds for identity in walk.image_ids}
    standalone_images = sum(image.id not in walkaround_images for image in selected)
    rust = summary.get('rust')
    rust_eligible = bool(rust and positive >= 10 and negative >= 10 and not missing)
    smoke = ('not_evaluated' if not rust_eligible else
             'passed_offline' if rust['recall'] is not None and rust['recall'] >= .60 else 'failed_offline')
    quality_eligible = split == 'heldout' and standalone_images >= 100 and len(walks) >= 3 and not missing
    per_concept_gates: dict[str,str] = {}
    for concept,metric in summary.items():
        precision,recall = ((.85,.75) if concept == 'rust' else (.80,.65))
        per_concept_gates[concept] = ('not_evaluated' if not quality_eligible or not metric['evaluable'] else
            'passed_offline' if metric['precision'] is not None and metric['precision'] >= precision
                and metric['recall'] is not None and metric['recall'] >= recall else 'failed_offline')
    quality = ('not_evaluated' if not quality_eligible or any(v == 'not_evaluated' for v in per_concept_gates.values())
        else 'passed_offline' if all(v == 'passed_offline' for v in per_concept_gates.values()) else 'failed_offline')
    failures = []
    if missing:
        failures.append('missing_prediction_images')
    if positive < 10 or negative < 10:
        failures.append('insufficient_rust_smoke_images')
    if standalone_images < 100:
        failures.append('insufficient_heldout_images')
    if len(walks) < 3:
        failures.append('insufficient_complete_walkaround_cars')
    if smoke == 'failed_offline':
        failures.append('rust_smoke_recall_below_provisional_threshold')
    for concept,metric in summary.items():
        if not metric['evaluable']:
            failures.append('no_positive_examples:'+concept)
        if per_concept_gates[concept] == 'failed_offline':
            failures.append('heldout_threshold_failed:'+concept)
    return {'schema_version':'upclose-offline-evaluation-receipt-v1','failures':failures,
        'input_hashes':{'dataset':dataset_sha256,'predictions':predictions_sha256,
            'metric_config':input_hash(json.dumps(METRIC_CONFIG,sort_keys=True,separators=(',',':')).encode())},
        'metric_config':METRIC_CONFIG,'split':split,
        'declared_provenance':{'dataset':dataset.provenance,'predictions':predictions.provenance},
        'provenance_verified':False,'provider_model_id':predictions.provider_model_id,
        'prompt_revision':predictions.prompt_revision,'prompt_sha256':predictions.prompt_sha256,
        'prediction_config_sha256':predictions.config_sha256,
        'captured_receipt_sha256':predictions.captured_receipt_sha256,
        'counts':{'images':len(selected),'standalone_images':standalone_images,'cars':len({i.car_id for i in selected}),
            'rust_positive_images':positive,'rust_clean_or_confounder_images':negative,
            'complete_walkaround_cars':len(walks),'missing_prediction_images':len(missing)},
        'per_concept':summary,'per_image':per_image,'missing_prediction_images':missing,
        'severity':{**severity,'exact_agreement':severity['exact']/severity['compared'] if severity['compared'] else None},
        'gates':{'offline_rust_smoke':smoke,'offline_heldout_quality':quality,
            'offline_heldout_per_concept':per_concept_gates,'live_feasibility':'unverified',
            'live_quality':'unverified','standard_access':'unverified','provider_prices':'unverified',
            'latency':'unverified','spend':'unverified'},
        'limitations':['Offline comparison of supplied declarations; no live transport or provenance verification.',
            'Missing predictions block offline gates and count labeled positives as false negatives.',
            'Provisional thresholds are diagnostic, not a production accuracy claim.']}
