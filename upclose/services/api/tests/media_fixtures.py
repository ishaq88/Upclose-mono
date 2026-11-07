"""Synthetic media generators for the media tests.

Everything is produced at test time with ffmpeg lavfi sources into a caller-supplied directory.
Provenance is always `synthetic`; no binaries are committed and no real vehicle footage is used.
"""

import random
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

PROVENANCE: Final[str] = "synthetic"
COLOR_STEPS: Final[tuple[tuple[str, tuple[int, int, int]], ...]] = (
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
    ("yellow", (255, 255, 0)),
)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "0x{:02X}{:02X}{:02X}".format(*rgb)
Container = Literal["mp4", "webm"]


@dataclass(frozen=True, slots=True)
class SyntheticClip:
    path: Path
    description: str
    provenance: str = PROVENANCE


def run_ffmpeg(*args: str) -> None:
    """Trusted test-side generator call (argument list, no shell)."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
        timeout=120,
    )


def _video_codec(container: Container) -> list[str]:
    if container == "webm":
        return ["-c:v", "libvpx", "-b:v", "600k", "-deadline", "realtime", "-cpu-used", "8", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "5"]


def make_pattern_clip(
    path: Path,
    *,
    container: Container = "mp4",
    size: tuple[int, int] = (320, 180),
    fps: int = 10,
    seconds: float = 2.0,
    video_filter: str | None = None,
    with_audio: bool = False,
) -> SyntheticClip:
    """Detailed test pattern (sharp edges); optional filter (e.g. boxblur) and audio track."""
    source = f"testsrc=s={size[0]}x{size[1]}:r={fps}:d={seconds}"
    args = ["-f", "lavfi", "-i", source]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if video_filter:
        args += ["-vf", video_filter]
    audio = ["-c:a", "libopus" if container == "webm" else "aac", "-shortest"] if with_audio else []
    run_ffmpeg(*args, *_video_codec(container), *audio, str(path))
    return SyntheticClip(path, f"testsrc {size[0]}x{size[1]} {fps}fps {seconds}s {container}")


def make_color_steps(path: Path, *, container: Container = "mp4", size: tuple[int, int] = (160, 90), fps: int = 10) -> SyntheticClip:
    """Four one-second solid colors (red, green, blue, yellow): frame i has color COLOR_STEPS[i // fps]."""
    inputs: list[str] = []
    for _, rgb in COLOR_STEPS:
        inputs += ["-f", "lavfi", "-i", f"color=c={_hex(rgb)}:s={size[0]}x{size[1]}:r={fps}:d=1"]
    graph = "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]"
    run_ffmpeg(*inputs, "-filter_complex", graph, "-map", "[v]", *_video_codec(container), str(path))
    return SyntheticClip(path, f"color steps {size[0]}x{size[1]} {fps}fps {container}")


def make_gap_clip(path: Path, *, size: tuple[int, int] = (160, 90)) -> SyntheticClip:
    """Variable frame rate: 5 red frames at 10 fps, then 5 blue frames after a 0.5 s timestamp gap."""
    inputs = [
        "-f", "lavfi", "-i", f"color=c=0xFF0000:s={size[0]}x{size[1]}:r=10:d=0.5",
        "-f", "lavfi", "-i", f"color=c=0x0000FF:s={size[0]}x{size[1]}:r=10:d=0.5",
    ]
    graph = "[0:v][1:v]concat=n=2:v=1:a=0,setpts='if(gte(N,5),PTS+0.5/TB,PTS)'[v]"
    run_ffmpeg(*inputs, "-filter_complex", graph, "-map", "[v]", "-fps_mode", "passthrough", *_video_codec("mp4"), str(path))
    return SyntheticClip(path, "variable frame rate red/blue with timestamp gap")


def make_rotated_clip(path: Path, display_clockwise: int, *, size: tuple[int, int] = (160, 90)) -> SyntheticClip:
    """Landscape-stored clip: blue frame with a red band on the LEFT edge, plus rotation metadata.

    The metadata makes a player rotate the stored frame `display_clockwise` degrees clockwise.
    ffmpeg 5.1 writes the `rotate` tag as a display matrix whose ffprobe rotation equals the tag
    value (counterclockwise), so the tag is the negated clockwise angle.
    """
    plain = path.with_name(path.stem + "-plain.mp4")
    band = size[0] // 4
    run_ffmpeg(
        "-f", "lavfi", "-i", f"color=c=0x0000FF:s={size[0]}x{size[1]}:r=10:d=1,drawbox=x=0:y=0:w={band}:h=ih:color=0xFF0000:t=fill",
        *_video_codec("mp4"), str(plain),
    )
    run_ffmpeg("-i", str(plain), "-c", "copy", "-metadata:s:v:0", f"rotate={(360 - display_clockwise) % 360}", str(path))
    plain.unlink()
    return SyntheticClip(path, f"left-band clip with {display_clockwise} degrees clockwise display rotation")


def make_band_clip(path: Path, *, size: tuple[int, int] = (160, 90)) -> SyntheticClip:
    """Same stored frame as `make_rotated_clip` but with no rotation metadata."""
    band = size[0] // 4
    run_ffmpeg(
        "-f", "lavfi", "-i", f"color=c=0x0000FF:s={size[0]}x{size[1]}:r=10:d=1,drawbox=x=0:y=0:w={band}:h=ih:color=0xFF0000:t=fill",
        *_video_codec("mp4"), str(path),
    )
    return SyntheticClip(path, "left-band clip without rotation metadata")


def make_headerless_webm(path: Path, *, seconds: float = 2.0) -> SyntheticClip:
    """WebM written to a pipe, so the muxer cannot seek back to write a duration (like MediaRecorder)."""
    completed = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=s=160x90:r=10:d={seconds}",
         *_video_codec("webm"), "-f", "webm", "pipe:1"],
        check=True,
        capture_output=True,
        timeout=120,
    )
    path.write_bytes(completed.stdout)
    return SyntheticClip(path, "webm without duration header")


def make_garbage(path: Path, size: int = 4096) -> SyntheticClip:
    path.write_bytes(random.Random(7).randbytes(size))
    return SyntheticClip(path, "random bytes")


def make_truncated(source: Path, path: Path, keep: float = 0.4) -> SyntheticClip:
    """A valid MP4 cut short before its moov atom (ffmpeg writes moov last by default)."""
    data = source.read_bytes()
    path.write_bytes(data[: int(len(data) * keep)])
    return SyntheticClip(path, "truncated mp4")


def make_audio_only(path: Path) -> SyntheticClip:
    run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", str(path))
    return SyntheticClip(path, "audio-only mp4")


def make_two_video_streams(path: Path) -> SyntheticClip:
    run_ffmpeg(
        "-f", "lavfi", "-i", "testsrc=s=160x90:r=10:d=1", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=1",
        "-map", "0:v", "-map", "1:v", *_video_codec("mp4"), str(path),
    )
    return SyntheticClip(path, "mp4 with two video streams")


def make_codec_clip(path: Path, codec: str = "mpeg4") -> SyntheticClip:
    run_ffmpeg("-f", "lavfi", "-i", "testsrc=s=160x90:r=10:d=1", "-c:v", codec, str(path))
    return SyntheticClip(path, f"mp4 with {codec} video")


def make_mpegts(path: Path) -> SyntheticClip:
    run_ffmpeg("-f", "lavfi", "-i", "testsrc=s=160x90:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", str(path))
    return SyntheticClip(path, "mpegts container")


def make_solid(path: Path, color: str, *, size: tuple[int, int] = (320, 180), seconds: float = 2.0) -> SyntheticClip:
    run_ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s={size[0]}x{size[1]}:r=10:d={seconds}", *_video_codec("mp4"), str(path))
    return SyntheticClip(path, f"solid {color}")


def make_long_tiny_clip(path: Path, seconds: int = 62) -> SyntheticClip:
    """Over-duration clip that is cheap to encode: tiny frames at 1 fps."""
    run_ffmpeg("-f", "lavfi", "-i", f"testsrc=s=16x16:r=1:d={seconds}", *_video_codec("mp4"), str(path))
    return SyntheticClip(path, f"{seconds}s tiny clip")
