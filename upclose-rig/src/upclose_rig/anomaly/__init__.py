"""Baseline-relative engine anomaly detection. numpy only.

Learn what "normal" looks like for one engine from a healthy baseline, then
score new windows by their regularized Mahalanobis distance from it.
"""

from .features import AnomalyError
from .model import AnomalyModel, assess_records, fit_from_records
from .stream import StreamScorer

__all__ = ["AnomalyError", "AnomalyModel", "StreamScorer", "assess_records", "fit_from_records"]
