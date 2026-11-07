"""Bounded solid redaction and finding annotation. No automated privacy claim."""
import math
import warnings
from pathlib import Path
from PIL import Image, ImageDraw
from app.contracts.evidence import BoundingBox

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 16_000_000


def _render_derivative(source: Path, target: Path, *, width: int, height: int,
                      bbox: BoundingBox, redactions: tuple[BoundingBox, ...], mask: Path | None = None) -> None:
    if source.stat().st_size > MAX_IMAGE_BYTES or len(redactions) > 100:
        raise ValueError("image bounds")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(source) as image:
            if image.width * image.height > MAX_PIXELS or image.size != (width, height) or getattr(image, "n_frames", 1) != 1:
                raise ValueError("image geometry")
            image.load()
            result = image.convert("RGB")
    if mask is not None:  # exact SAM raster, box-local; never stretched onto the box
        with Image.open(mask) as overlay:
            box_w = max(1, round((bbox.x + bbox.w) * width) - round(bbox.x * width))
            box_h = max(1, round((bbox.y + bbox.h) * height) - round(bbox.y * height))
            if overlay.size != (box_w, box_h):
                raise ValueError("mask geometry")
            tint = Image.new("RGB", overlay.size, (255, 190, 0))
            left, top = round(bbox.x * width), round(bbox.y * height)
            region = result.crop((left, top, left + box_w, top + box_h))
            result.paste(Image.blend(region, tint, 0.45), (left, top),
                         overlay.point(lambda value: 255 if value else 0))
    draw = ImageDraw.Draw(result)
    def rectangle(box: BoundingBox) -> tuple[int, int, int, int]:
        return (math.floor(box.x * width), math.floor(box.y * height),
                min(width - 1, math.ceil((box.x + box.w) * width)),
                min(height - 1, math.ceil((box.y + box.h) * height)))
    # Annotation first, redaction last: overlays never overwrite a privacy mask.
    draw.rectangle(rectangle(bbox), outline=(255, 190, 0), width=max(2, width // 300))
    for box in redactions:
        draw.rectangle(rectangle(box), fill=(0, 0, 0))
    result.save(target, format="PNG", optimize=False)
    if target.stat().st_size > MAX_IMAGE_BYTES:
        target.unlink()
        raise ValueError("image bounds")



def render_derivative(source: Path, target: Path, *, width: int, height: int,
                      bbox: BoundingBox, redactions: tuple[BoundingBox, ...], mask: Path | None = None) -> None:
    try:
        _render_derivative(source, target, width=width, height=height, bbox=bbox, redactions=redactions, mask=mask)
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        target.unlink(missing_ok=True)
        raise ValueError('derivative rendering failed') from None
