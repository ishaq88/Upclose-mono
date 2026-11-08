"""NHTSA vPIC VIN decoder over the official `DecodeVinValues` endpoint.

Official documentation: https://vpic.nhtsa.dot.gov/api/ (checked 2026-11-07).
- `GET https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{VIN}?format=json`
  (optional `&modelyear=YYYY`, which the docs recommend when the year is known).
  The `modelyear` parameter is omitted here: the position-10 year code repeats every
  30 years and this adapter must not guess between the candidates.
- Flat response: `{"Results": [{"VIN", "Make", "Model", "ModelYear", "BodyClass",
  "EngineCylinders", "DisplacementCC"/"DisplacementCI"/"DisplacementL",
  "FuelTypePrimary", "ErrorCode", "ErrorText", ...}]}`; `ErrorCode "0"` is a clean decode.
- Free, no API key. "Applications attempting to use vPIC APIs will be controlled by
  an automated traffic rate control mechanism" (same docs page); this adapter makes
  one unretried call per decode with a 5 s timeout and returns `None` with a reason
  on any failure. Only the listed fields are extracted; raw provider text never
  reaches the caller.
"""

from __future__ import annotations

import json

from app.domain.vin import VinError, is_well_formed, normalize_vin

from . import TIMEOUT_SECONDS, Transport, VinDecode, VinFetchError, VinOutcome, urllib_get

API_BASE = "https://vpic.nhtsa.dot.gov/api/vehicles"


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _displacement_l(row: dict) -> float | None:
    litres = _text(row.get("DisplacementL"))
    if litres is not None:
        try:
            return round(float(litres), 3)
        except ValueError:
            pass
    cc = _text(row.get("DisplacementCC"))
    if cc is not None:
        try:
            return round(float(cc) / 1000.0, 3)
        except ValueError:
            pass
    ci = _text(row.get("DisplacementCI"))
    if ci is not None:
        try:
            return round(float(ci) * 0.0163871, 3)
        except ValueError:
            pass
    return None


class NhtsaVin:
    def __init__(self, transport: Transport = urllib_get) -> None:
        self._transport = transport

    def decode(self, vin: str) -> VinOutcome:
        try:
            normalized = normalize_vin(vin)
        except VinError:
            return VinOutcome(decode=None, reason="malformed VIN")
        if not is_well_formed(normalized):
            return VinOutcome(decode=None, reason="malformed VIN")
        try:
            status, raw = self._transport(
                f"{API_BASE}/DecodeVinValues/{normalized}?format=json", TIMEOUT_SECONDS)
        except VinFetchError:
            return VinOutcome(decode=None, reason="vpic request timed out")
        except Exception:
            return VinOutcome(decode=None, reason="vpic request failed")
        if status != 200:
            return VinOutcome(decode=None, reason=f"vpic returned HTTP {status}")
        try:
            payload = json.loads(raw)
        except ValueError:
            return VinOutcome(decode=None, reason="vpic returned malformed data")
        results = payload.get("Results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return VinOutcome(decode=None, reason="vpic returned no results")
        row = results[0]
        error_code = str(row.get("ErrorCode", "")).strip()
        make = _text(row.get("Make"))
        if error_code not in ("0", "") and not make:
            detail = (_text(row.get("ErrorText")) or f"code {error_code}")[:120]
            return VinOutcome(decode=None, reason=f"vpic could not decode VIN ({detail})")
        return VinOutcome(decode=VinDecode(make=make, model=_text(row.get("Model")),
                                           model_year=_int(row.get("ModelYear")),
                                           cylinders=_int(row.get("EngineCylinders")),
                                           displacement_l=_displacement_l(row),
                                           fuel_type=_text(row.get("FuelTypePrimary")),
                                           body_class=_text(row.get("BodyClass")),
                                           source="nhtsa-vpic"),
                          reason=None)
