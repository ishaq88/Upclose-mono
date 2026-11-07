"""Derivative rendering: SAM mask overlay is exact, redactions still win."""
from pathlib import Path

from PIL import Image

from app.contracts.evidence import BoundingBox
from app.pipeline.derivatives import render_derivative


def test_mask_overlay_tints_only_masked_pixels_and_redaction_wins(tmp_path: Path) -> None:
    source, target, mask = tmp_path / "s.png", tmp_path / "t.png", tmp_path / "m.png"
    Image.new("RGB", (100, 100), (10, 10, 10)).save(source, format="PNG")
    overlay = Image.new("L", (20, 20), 0)
    overlay.putpixel((5, 5), 255)
    overlay.save(mask, format="PNG")
    bbox = BoundingBox(x=0.1, y=0.1, w=0.2, h=0.2)
    render_derivative(source, target, width=100, height=100, bbox=bbox, redactions=(), mask=mask)
    with Image.open(target) as result:
        assert result.getpixel((0, 0)) == (10, 10, 10)  # outside box untouched
        inside = result.getpixel((15, 15))
        assert inside[0] > 100 and inside[2] < 10  # masked pixel tinted amber
        assert result.getpixel((20, 20)) == (10, 10, 10)  # unmasked in-box pixel untouched
    render_derivative(source, target, width=100, height=100, bbox=bbox,
                      redactions=(BoundingBox(x=0.1, y=0.1, w=0.2, h=0.2),), mask=mask)
    with Image.open(target) as result:
        assert result.getpixel((15, 15)) == (0, 0, 0)  # redaction drawn last


def test_mask_size_mismatch_fails_closed(tmp_path: Path) -> None:
    source, target, mask = tmp_path / "s.png", tmp_path / "t.png", tmp_path / "m.png"
    Image.new("RGB", (100, 100), (10, 10, 10)).save(source, format="PNG")
    Image.new("L", (3, 3), 255).save(mask, format="PNG")
    try:
        render_derivative(source, target, width=100, height=100,
                          bbox=BoundingBox(x=0.1, y=0.1, w=0.2, h=0.2), redactions=(), mask=mask)
        raise AssertionError("mismatched mask must fail")
    except Exception:
        assert not target.exists()
