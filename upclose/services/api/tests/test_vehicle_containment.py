"""Vehicle containment math (stage 1.5). Pure: no provider, network, or database."""
from app.adapters.meta.types import MaskRaster
from app.pipeline.segment import containment_fraction

W, H = 10, 10
BBOX = {"x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2}  # 2x2 box at origin


def mask(pixels: list[int]) -> MaskRaster:
    return MaskRaster(width=2, height=2, pixels=bytes(pixels))


def coverage(points: set[tuple[int, int]]) -> bytes:
    values = bytearray(W * H)
    for x, y in points:
        values[y * W + x] = 1
    return bytes(values)


def test_fully_inside_and_outside() -> None:
    full = coverage({(0, 0), (1, 0), (0, 1), (1, 1)})
    assert containment_fraction(mask([1, 1, 1, 1]), BBOX, full, W, H) == 1.0
    assert containment_fraction(mask([1, 1, 1, 1]), BBOX, coverage({(5, 5)}), W, H) == 0.0


def test_straddling_fraction() -> None:
    half = coverage({(0, 0), (1, 0)})
    assert containment_fraction(mask([1, 1, 1, 1]), BBOX, half, W, H) == 0.5


def test_no_basis_returns_none() -> None:
    assert containment_fraction(mask([1, 1, 1, 1]), BBOX, None, W, H) is None
    assert containment_fraction(mask([1, 1, 1, 1]), BBOX, bytes(W * H), W, H) is None
    assert containment_fraction(mask([0, 0, 0, 0]), BBOX, coverage({(0, 0)}), W, H) is None


def test_misaligned_geometry_has_no_basis() -> None:
    full = coverage({(0, 0), (1, 0), (0, 1), (1, 1)})
    assert containment_fraction(mask([1, 1, 1, 1]), BBOX, full[:-1], W, H) is None
    wrong_extent = MaskRaster(width=1, height=1, pixels=b"\x01")
    assert containment_fraction(wrong_extent, BBOX, full, W, H) is None
