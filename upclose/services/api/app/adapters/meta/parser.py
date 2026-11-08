"""Strict normalization over Meta's pinned public parser, not a guessed codec."""
import logging
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from uuid import UUID

from meta_sam_parser import (
    CompletedOutcome, SegmentationBoxRecord, SegmentationMaskRecord,
    image_segmentation_format, video_segmentation_format,
)
from meta_sam_parser import _segmentation as _vendor_segmentation

from .types import MaskRaster, MediaRef, ProviderUsage, SegmentObservation, SegmentResult


class ProviderFailure(Exception):
    """Safe stable code only: never include provider output or media references.

    ``http_status`` is the optional transport HTTP status (int 100-599) when the
    failure came from an HTTP response; it carries no payload, header or key text.
    Network failures and non-HTTP failures leave it ``None``.
    """
    def __init__(self, code: str, *, http_status: int | None = None) -> None:
        self.code = code
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        super().__init__(code)

    @property
    def http_status_class(self) -> str | None:
        return None if self.http_status is None else f"{self.http_status // 100}xx"

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


# Transport-level outcomes only. Every other code describes a response that was already
# billed; re-calling with the same input repeats the same rejection and the same charge.
RETRYABLE_CODES = frozenset({"provider_timeout", "provider_failed", "incomplete_response"})


MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_MASK_PIXELS = 1280 * 1280
MAX_RECORDS = 16000


def _per_mask_pixel_limit(media: MediaRef) -> int:
    width, height = media.display_size
    return min(MAX_MASK_PIXELS, width * height)


def _total_mask_pixel_limit(media: MediaRef) -> int:
    # Frame-aware DoS bound: each frame may contribute at most one
    # maximum-size mask, so the total scales with the frame count.
    return MAX_MASK_PIXELS * len(media.frames)


_BOX = re.compile(r"<\|box;([^|]*)\|>")
_MASK = re.compile(r"<\|mask;([^|]*)\|>")


def _fields(body: str) -> dict[str, str]:
    items = body.split(";")
    result: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or key in result:
            raise ProviderFailure("malformed_record")
        result[key] = value
    return result


def _resample(raster: bytes, width: int, height: int, to_width: int, to_height: int) -> bytes:
    """Nearest-neighbour resize of a row-major binary raster; stays binary."""
    columns = [x * width // to_width for x in range(to_width)]
    rows: dict[int, bytes] = {}
    out = bytearray()
    for y in range(to_height):
        source = y * height // to_height
        if source not in rows:
            row = raster[source * width:(source + 1) * width]
            rows[source] = bytes(map(row.__getitem__, columns))
        out += rows[source]
    return bytes(out)


def _preflight(output: str, media: MediaRef) -> None:
    if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ProviderFailure("output_limit")
    boxes = list(_BOX.finditer(output))
    masks = list(_MASK.finditer(output))
    if len(boxes) > MAX_RECORDS or len(masks) > MAX_RECORDS:
        raise ProviderFailure("output_limit")
    try:
        for token in boxes:
            fields = _fields(token.group(1))
            if (int(fields["w"]), int(fields["h"])) != media.display_size:
                raise ProviderFailure("source_dimensions_mismatch")
        per_mask_limit = _per_mask_pixel_limit(media)
        total_limit = _total_mask_pixel_limit(media)
        total_area = 0
        for token in masks:
            fields = _fields(token.group(1))
            height, width, _ = fields["data"].split(",", 2)
            area = int(height) * int(width)
            if int(height) <= 0 or int(width) <= 0 or area > per_mask_limit:
                raise ProviderFailure("mask_limit")
            total_area += area
            if total_area > total_limit:
                raise ProviderFailure("mask_limit")
    except (KeyError, ValueError) as error:
        raise ProviderFailure("malformed_record") from None


_DECODE_LOCK = threading.RLock()


@contextmanager
def _decode_each_mask_once() -> Iterator[Callable[..., bytes]]:
    """Yield a decoder that the official parser also uses, so each mask is decoded once.

    The official parser decodes every mask to validate it and then drops the bytes; we need the
    bytes as well. The lossless codec is pure Python (about 2-3 s per full-size mask), so decoding
    twice doubled the response parse time (measured: 12 of 24 decodes were repeats). The parser's
    own reference is swapped for a memoizing wrapper for the length of one call: same function,
    same inputs, same bytes, same errors (failures are not cached), just no repeat work.
    """
    original = _vendor_segmentation.decode_mask_to_raster
    cache: dict[tuple[object, str, int, int], bytes] = {}

    def decode(mask):  # type: ignore[no-untyped-def]
        key = (mask.encoding, mask.payload, mask.width, mask.height)
        if key not in cache:
            cache[key] = original(mask)
        return cache[key]

    with _DECODE_LOCK:
        _vendor_segmentation.decode_mask_to_raster = decode
        try:
            yield decode
        finally:
            _vendor_segmentation.decode_mask_to_raster = original


def normalize_output(
    output: str, media: MediaRef, prompt_id: str, request_id: UUID,
    *, usage: ProviderUsage, provenance: str = "synthetic", completed: bool = True,
) -> SegmentResult:
    if not completed:
        raise ProviderFailure("incomplete_response")
    _preflight(output, media)
    format_ = image_segmentation_format if media.kind == "image" else video_segmentation_format
    with _decode_each_mask_once() as decode:
        return _normalize_parsed(output, media, prompt_id, request_id, usage, provenance, format_, decode)


def _normalize_parsed(output, media, prompt_id, request_id, usage, provenance, format_, decode_mask):  # type: ignore[no-untyped-def]
    parser = format_().create_parser()
    try:
        parser.push(output)
        parsed = parser.finish(CompletedOutcome()).result
    except Exception:
        raise ProviderFailure("parser_error") from None
    # The upstream parser is permissive: a bad line can coexist with success.
    # Reject every error so dropped candidates cannot silently look clean.
    # Warnings (ignored unknown fields/tokens) keep their record and are not fatal.
    if any(item.severity != "warning" for item in parsed.diagnostics):
        raise ProviderFailure("invalid_segmentation")
    frames = {frame.frame_index: frame for frame in media.frames}
    boxes: dict[tuple[int, str], SegmentationBoxRecord] = {}
    masks: dict[tuple[int, str], SegmentationMaskRecord] = {}
    for record in parsed.records:
        if not isinstance(record, (SegmentationBoxRecord, SegmentationMaskRecord)):
            raise ProviderFailure("unexpected_output_text")
        index = record.frame.frame_index if record.frame is not None else 0
        if index not in frames:
            raise ProviderFailure("unknown_source_frame")
        if not record.object_id.isascii() or not record.object_id.isdigit() or len(record.object_id) > 128:
            raise ProviderFailure("invalid_track_id")
        key = (index, record.object_id)
        target = boxes if isinstance(record, SegmentationBoxRecord) else masks
        # A re-emitted (frame, object) is the official parser's next revision; the latest wins.
        target[key] = record
    if boxes.keys() != masks.keys():
        raise ProviderFailure("unpaired_geometry")
    width, height = media.display_size
    observations: list[SegmentObservation] = []
    for key in sorted(boxes, key=lambda pair: (pair[0], int(pair[1]))):
        box, mask = boxes[key], masks[key]
        frame = frames[key[0]]
        if box.confidence != mask.confidence:
            raise ProviderFailure("confidence_mismatch")
        bounds = mask.bounds
        if (bounds.left, bounds.top, bounds.right, bounds.bottom) != (box.left, box.top, box.right, box.bottom):
            raise ProviderFailure("mask_alignment_mismatch")
        # The raster is box-local (origin x=0;y=0 at the box corner) but Meta does not
        # promise it is at the box's pixel extent; a differently sized raster is
        # resampled onto the box, never placed on another frame or offset.
        box_width, box_height = box.right - box.left, box.bottom - box.top
        try:
            raster = decode_mask(mask.mask)
            if (mask.mask.width, mask.mask.height) != (box_width, box_height):
                logging.warning("SAM mask resampled onto box: raster=%dx%d box=%dx%d frame=%dx%d",
                                mask.mask.width, mask.mask.height, box_width, box_height, width, height)
                raster = _resample(raster, mask.mask.width, mask.mask.height, box_width, box_height)
            observation = SegmentObservation(
                track_id=key[1], frame_index=key[0], frame_key=frame.frame_key,
                timestamp_ms=frame.timestamp_ms, frame_width=width, frame_height=height,
                bbox={"x": box.left / width, "y": box.top / height,
                      "w": box_width / width, "h": box_height / height},
                mask=MaskRaster(width=box_width, height=box_height, pixels=raster),
                confidence=box.confidence,
            )
        except Exception:
            raise ProviderFailure("invalid_geometry") from None
        observations.append(observation)
    counts: dict[int, int] = {}
    for index, _ in boxes:
        counts[index] = counts.get(index, 0) + 1
    limits = ("documented_video_track_cap_reached",) if media.kind == "video" and any(
        count >= 16 for count in counts.values()
    ) else ()
    return SegmentResult(request_id=request_id, prompt_id=prompt_id, provenance=provenance,
                         observations=tuple(observations), usage=usage, limits_encountered=limits)
