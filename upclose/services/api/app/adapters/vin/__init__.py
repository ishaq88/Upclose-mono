"""VIN decode providers. `VIN_PROVIDER=fixture` (default) or `nhtsa` (vPIC decode).

Provider failure returns `None` with a reason; it never raises and never passes raw
provider text through. Full VINs must not be logged; errors name the failure only.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

Transport = Callable[[str, float], tuple[int, bytes]]

VinSource = Literal["fixture", "nhtsa-vpic"]
TIMEOUT_SECONDS = 5.0


class VinConfigError(RuntimeError):
    pass


class VinFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class VinDecode:
    make: str | None
    model: str | None
    model_year: int | None
    cylinders: int | None
    displacement_l: float | None
    fuel_type: str | None
    body_class: str | None
    source: VinSource


@dataclass(frozen=True)
class VinOutcome:
    decode: VinDecode | None
    reason: str | None


class VinAdapter(Protocol):
    def decode(self, vin: str) -> VinOutcome: ...


def urllib_get(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VinFetchError("request timed out") from exc


def get_vin_adapter(environ: Mapping[str, str] | None = None,
                    transport: Transport | None = None) -> VinAdapter:
    from .config import load_env
    env = load_env(environ)
    if env.provider == "fixture":
        from .fixture import FixtureVin
        return FixtureVin()
    from .nhtsa import NhtsaVin
    return NhtsaVin(transport or urllib_get)
