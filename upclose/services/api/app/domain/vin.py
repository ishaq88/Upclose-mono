"""VIN normalization, validation, check digit, model year, WMI lookup, comparison. Pure.

No network, ORM, or framework imports. VINs handled here may identify a real vehicle;
callers must never log or report a full VIN where the free report or logs can expose it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")  # I, O, Q are never used in a VIN

_TRANSLITERATION = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
                    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
                    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9}
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)

# Position-10 model-year codes, 1980-2009 base. The same 30 codes repeat every 30 years,
# so every code maps to a pair of candidate years; callers must not guess between them.
_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"
_YEAR_BASE = 1980

# Sample WMI (first 3 VIN characters) table: manufacturer and build region. WMI
# assignments are maintained by SAE International under ISO 3780; these entries were
# cross-checked against the NHTSA vPIC WMI decoder
# (https://vpic.nhtsa.dot.gov/api/vehicles/DecodeWMI/1HG?format=json).
# It is a hint table only -- unknown WMIs return None and the vPIC provider is authoritative.
_WMI_TABLE = {
    "1G1": ("Chevrolet", "United States"),
    "1HG": ("Honda", "United States"),
    "19X": ("Honda", "United States"),
    "2HG": ("Honda", "Canada"),
    "3VW": ("Volkswagen", "Mexico"),
    "4T1": ("Toyota", "United States"),
    "5YJ": ("Tesla", "United States"),
    "JH4": ("Acura", "Japan"),
    "JT2": ("Toyota", "Japan"),
    "KMH": ("Hyundai", "South Korea"),
    "WBA": ("BMW", "Germany"),
    "WVW": ("Volkswagen", "Germany"),
    "VF1": ("Renault", "France"),
    "YS3": ("Saab", "Sweden"),
    "ZFA": ("Fiat", "Italy"),
    "SAL": ("Land Rover", "United Kingdom"),
}

CheckStatus = Literal["valid", "invalid", "not_applicable"]
CompareStatus = Literal["match", "mismatch", "not_checked"]


class VinError(ValueError):
    """Malformed VIN input; message never contains the VIN itself."""


def normalize_vin(raw: object) -> str:
    """Strip surrounding whitespace, drop internal spaces/hyphens, uppercase."""
    if not isinstance(raw, str):
        raise VinError("VIN must be a string")
    return re.sub(r"[\s-]+", "", raw.strip().upper())


def is_well_formed(vin: str) -> bool:
    """17 characters, no I/O/Q."""
    return bool(_VIN_RE.match(vin))


def _require_well_formed(vin: str) -> str:
    normalized = normalize_vin(vin)
    if not is_well_formed(normalized):
        raise VinError("VIN must be 17 characters without I, O, or Q")
    return normalized


def expected_check_digit(vin: str) -> str:
    """Position-9 check digit for a well-formed VIN (North American rule)."""
    normalized = _require_well_formed(vin)
    total = sum((_TRANSLITERATION[char] if char.isalpha() else int(char)) * weight
                 for char, weight in zip(normalized, _WEIGHTS))
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def is_north_american(vin: str) -> bool:
    """First character 1-5; only these VINs fall under the check-digit rule."""
    return _require_well_formed(vin)[0] in "12345"


def check_digit_status(vin: str) -> CheckStatus:
    """`valid`/`invalid` for North American VINs; `not_applicable` elsewhere.

    Non-North-American VINs may not use the check digit, so they are never
    called invalid on that basis alone.
    """
    normalized = _require_well_formed(vin)
    if normalized[0] not in "12345":
        return "not_applicable"
    return "valid" if normalized[8] == expected_check_digit(normalized) else "invalid"


def model_year_candidates(vin: str) -> tuple[int, ...]:
    """Candidate model years from position 10, 30 years apart. Empty when unknown."""
    normalized = _require_well_formed(vin)
    index = _YEAR_CODES.find(normalized[9])
    if index < 0:
        return ()
    return (_YEAR_BASE + index, _YEAR_BASE + index + 30)


@dataclass(frozen=True)
class WmiInfo:
    manufacturer: str | None
    region: str | None


def wmi_info(vin: str) -> WmiInfo:
    """Manufacturer/region hint from the sample WMI table; unknown WMIs give Nones."""
    normalized = _require_well_formed(vin)
    entry = _WMI_TABLE.get(normalized[:3])
    if entry is None:
        return WmiInfo(manufacturer=None, region=None)
    return WmiInfo(manufacturer=entry[0], region=entry[1])


@dataclass(frozen=True)
class VinFacts:
    vin: str
    check: CheckStatus
    years: tuple[int, ...]
    manufacturer: str | None
    region: str | None


def summarize_vin(vin: str) -> VinFacts:
    """All local facts for a well-formed VIN in one record."""
    normalized = _require_well_formed(vin)
    info = wmi_info(normalized)
    return VinFacts(vin=normalized, check=check_digit_status(normalized),
                    years=model_year_candidates(normalized),
                    manufacturer=info.manufacturer, region=info.region)


def compare_vins(entered: object, obd: object) -> CompareStatus:
    """Compare an inspector-entered VIN with the OBD-reported one.

    Anything missing or malformed on either side is `not_checked`, never a mismatch.
    """
    try:
        first = normalize_vin(entered) if isinstance(entered, str) and entered.strip() else ""
        second = normalize_vin(obd) if isinstance(obd, str) and obd.strip() else ""
    except VinError:
        return "not_checked"
    if not first or not second or not is_well_formed(first) or not is_well_formed(second):
        return "not_checked"
    return "match" if first == second else "mismatch"
