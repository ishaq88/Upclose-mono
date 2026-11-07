"""Shared strict wire primitives. No persistence or framework dependencies."""

from decimal import Decimal, ROUND_HALF_UP
from datetime import timezone
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _score(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


Score = Annotated[
    Decimal,
    Field(ge=0, le=100),
    PlainSerializer(lambda value: float(_score(value)), return_type=float, when_used="json"),
]
Severity = Annotated[int, Field(strict=True, ge=1, le=5)]
Count = Annotated[int, Field(strict=True, ge=0)]
Cents = Annotated[int, Field(strict=True, ge=0)]
MicroUSD = Annotated[int, Field(strict=True, ge=0)]
UTCDateTime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(timezone.utc))]
PartNumber = Annotated[int, Field(strict=True, ge=1, le=8)]
Confidence = Annotated[float, Field(ge=0, le=1)]
def _object_key(value: str) -> str:
    if "://" in value or "?" in value or "#" in value:
        raise ValueError("object references must be keys, not URLs")
    return value


ObjectKey = Annotated[
    str, Field(min_length=1, max_length=1024, pattern=r"^[^\r\n]+$"),
    AfterValidator(_object_key),
]


class FixtureMetadata(ContractModel):
    provenance: Annotated[str, Field(pattern="^synthetic$")]
    description: Annotated[str, Field(min_length=1, max_length=240)]
