"""Fixture VIN provider: local facts only (WMI table, year candidates). Never opens a socket."""

from __future__ import annotations

from app.domain.vin import VinError, is_well_formed, normalize_vin, wmi_info

from . import VinDecode, VinOutcome


class FixtureVin:
    def decode(self, vin: str) -> VinOutcome:
        try:
            normalized = normalize_vin(vin)
        except VinError:
            return VinOutcome(decode=None, reason="malformed VIN")
        if not is_well_formed(normalized):
            return VinOutcome(decode=None, reason="malformed VIN")
        info = wmi_info(normalized)
        return VinOutcome(decode=VinDecode(make=info.manufacturer, model=None, model_year=None,
                                           cylinders=None, displacement_l=None, fuel_type=None,
                                           body_class=None, source="fixture"),
                          reason=None)
