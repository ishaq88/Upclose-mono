"""Strict permission/label declarations and captured predictions; declarations are not verification."""
from typing import Annotated, Literal, Self
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.contracts.enums import Concept
from app.contracts.evidence import BoundingBox

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.-]+$')]
Hash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Split = Literal['tuning', 'heldout']
Provenance = Literal['synthetic', 'cached', 'live']

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)

class Box(StrictModel):
    id: Identifier
    concept: Concept
    component_id: Identifier
    bbox: BoundingBox
    severity: Annotated[int, Field(strict=True, ge=1, le=5)] | None = None

class LabeledImage(StrictModel):
    id: Identifier
    car_id: Identifier
    frame_id: Identifier
    media_sha256: Hash
    split: Split
    part_no: Annotated[int, Field(strict=True, ge=1, le=8)]
    permission: Literal['synthetic', 'license', 'consent']
    permission_receipt_sha256: Hash
    reviewer: Identifier
    blur: Literal['clear', 'blurry', 'mixed']
    lighting: Literal['daylight', 'artificial', 'low', 'mixed']
    reviewed_concepts: Annotated[tuple[Concept, ...], Field(min_length=1, max_length=7)]
    rust_category: Literal['positive', 'clean', 'confounder', 'not_reviewed']
    labels: Annotated[tuple[Box, ...], Field(max_length=200)]

    @model_validator(mode='after')
    def valid_labels(self) -> Self:
        if len({b.id for b in self.labels}) != len(self.labels) or len(set(self.reviewed_concepts)) != len(self.reviewed_concepts):
            raise ValueError('duplicate label or reviewed concept')
        if any(b.concept not in self.reviewed_concepts for b in self.labels):
            raise ValueError('label concept was not reviewed')
        has_rust = any(b.concept == Concept.RUST for b in self.labels)
        if Concept.RUST in self.reviewed_concepts:
            if self.rust_category == 'not_reviewed' or has_rust != (self.rust_category == 'positive'):
                raise ValueError('rust category and labels disagree')
        elif self.rust_category != 'not_reviewed':
            raise ValueError('rust category requires rust review')
        return self

class Walkaround(StrictModel):
    id: Identifier
    car_id: Identifier
    split: Split
    complete: Annotated[bool, Field(strict=True)]
    image_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=10000)]
    reviewer: Identifier
    source_video_sha256: Hash
    completeness_receipt_sha256: Hash

class DatasetManifest(StrictModel):
    schema_version: Literal['upclose-dataset-v1']
    provenance: Provenance
    images: Annotated[tuple[LabeledImage, ...], Field(min_length=1, max_length=10000)]
    walkarounds: Annotated[tuple[Walkaround, ...], Field(max_length=1000)] = ()

    @model_validator(mode='after')
    def identities_and_splits(self) -> Self:
        images = {image.id: image for image in self.images}
        if len(images) != len(self.images) or len({w.id for w in self.walkarounds}) != len(self.walkarounds):
            raise ValueError('duplicate image or walkaround')
        identities = {(i.car_id,i.frame_id) for i in self.images}
        if len(identities) != len(self.images):
            raise ValueError('duplicate car/frame identity')
        hashes = {i.media_sha256 for i in self.images}
        if len(hashes) != len(self.images):
            raise ValueError('duplicate media bytes cannot count as separate examples')
        splits: dict[str,str] = {}
        for image in self.images:
            if image.car_id in splits and splits[image.car_id] != image.split:
                raise ValueError('tuning and heldout car leakage')
            splits[image.car_id] = image.split
            if self.provenance != 'synthetic' and image.permission == 'synthetic':
                raise ValueError('non-synthetic provenance requires licensed or consented media declaration')
        if len({w.source_video_sha256 for w in self.walkarounds}) != len(self.walkarounds):
            raise ValueError('duplicate walkaround source video')
        claimed: set[str] = set()
        for walk in self.walkarounds:
            if len(set(walk.image_ids)) != len(walk.image_ids):
                raise ValueError('duplicate walkaround frame')
            for identity in walk.image_ids:
                image = images.get(identity)
                if image is None or image.car_id != walk.car_id or image.split != walk.split or identity in claimed:
                    raise ValueError('walkaround image ownership or split mismatch')
                claimed.add(identity)
            if walk.complete and {images[i].part_no for i in walk.image_ids} != set(range(1,9)):
                raise ValueError('complete walkaround requires evidence for all eight capture parts')
        return self

class ImagePredictions(StrictModel):
    image_id: Identifier
    frame_id: Identifier
    media_sha256: Hash
    predictions: Annotated[tuple[Box, ...], Field(max_length=200)]

    @model_validator(mode='after')
    def unique_predictions(self) -> Self:
        if len({b.id for b in self.predictions}) != len(self.predictions):
            raise ValueError('duplicate prediction identity')
        return self

class PredictionBundle(StrictModel):
    schema_version: Literal['upclose-predictions-v1']
    dataset_sha256: Hash
    provenance: Provenance
    provider_model_id: Identifier
    prompt_revision: Identifier
    prompt_sha256: Hash
    config_sha256: Hash
    captured_receipt_sha256: Hash
    enabled_concepts: Annotated[tuple[Concept, ...], Field(min_length=1, max_length=7)]
    images: Annotated[tuple[ImagePredictions, ...], Field(max_length=10000)]

    @model_validator(mode='after')
    def unique_images_and_concepts(self) -> Self:
        if len({i.image_id for i in self.images}) != len(self.images) or len(set(self.enabled_concepts)) != len(self.enabled_concepts):
            raise ValueError('duplicate prediction image or enabled concept')
        if any(b.concept not in self.enabled_concepts for image in self.images for b in image.predictions):
            raise ValueError('prediction uses a disabled concept')
        return self
