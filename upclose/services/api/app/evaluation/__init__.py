"""Offline evaluation: no transport, persistence, or authority to attest a live gate."""
from .schema import DatasetManifest, PredictionBundle
from .metrics import evaluate, input_hash, match_boxes
from .intake import IntakeManifest, validate_dataset_intake

__all__ = ['DatasetManifest', 'PredictionBundle', 'evaluate', 'input_hash', 'match_boxes',
           'IntakeManifest', 'validate_dataset_intake']
