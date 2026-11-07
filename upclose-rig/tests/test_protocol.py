import pytest

from upclose_rig.protocol import (
    HEADER_LINE, HeaderMissing, ProtocolError, Record, parse_line, parse_stream,
)
from upclose_rig.serial_io import capture


def test_good_lines():
    assert parse_line("A,12,-5,3,1001") == Record("A", 12, (-5, 3, 1001))
    assert parse_line("S,10,312\r\n") == Record("S", 10, (312,))
    assert parse_line("T,2000,21.4,35.5") == Record("T", 2000, (21.4, 35.5))
    assert parse_line("B,500,1") == Record("B", 500, (1,))
    assert parse_line("K,40,1023") == Record("K", 40, (1023,))
    assert parse_line("") is None
    assert parse_line("#boot ok") is None


@pytest.mark.parametrize(
    "bad",
    ["A,12,1,2", "A,12,1,2,3,4", "A,x,1,2,3", "A,1,1,2,z", "Z,1,2", "B,1,2", "S,1,1024",
     "K,1,-1", "T,1,nan,3", "A,-1,1,2,3", "A,1,1.5,2,3", "A12,1,2,3"],
)
def test_garbled_lines_raise(bad):
    with pytest.raises(ProtocolError):
        parse_line(bad)


def test_stream_skips_garbled_and_boot_noise():
    lines = ["\x00\x7f junk", HEADER_LINE, "A,1,1,2,3", "A,2,1,2", "S,3,5", "garbage"]
    items = list(parse_stream(lines))
    assert [type(i).__name__ for i in items] == ["Record", "ProtocolError", "Record", "ProtocolError"]


def test_missing_header_raises():
    with pytest.raises(HeaderMissing):
        list(parse_stream(["A,1,1,2,3", "S,2,3"]))


def test_wrong_version_raises():
    with pytest.raises(ProtocolError):
        list(parse_stream(["#upclose-pod v2", "A,1,1,2,3"]))


def test_capture_counts_dropped_and_stops_on_time():
    ticks = iter(range(100))
    lines = [HEADER_LINE, "A,1,1,2,3", "junk", "A,2,1,2,3", "A,3,1,2,3", "A,4,1,2,3"]
    rec, dropped = capture(lines, seconds=2, clock=lambda: float(next(ticks)))
    assert dropped == 1
    assert rec.source == "board" and rec.header["protocol"] == "upclose-pod v1"
    assert 1 <= len(rec.records) <= 3


def test_capture_without_header_fails():
    with pytest.raises(HeaderMissing):
        capture(["A,1,1,2,3"], seconds=1)
