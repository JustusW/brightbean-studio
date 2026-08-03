"""Make the smallest thing that is still honestly a video.

TWO PLATFORMS TAKE VIDEO AND NOTHING ELSE. TikTok and YouTube can be
installed, composed with and previewed without one - and then publish
nothing, which is exactly where they stood: connected, exercised, and never
once having sent a post anywhere.

WHY IT IS GENERATED AND NOT COMMITTED. A binary checked into the repository
is a file nobody can read in a diff and nobody can check: its provenance is a
commit message, and its contents are whatever was pasted that day. This is
built from two synthetic sources on every run, so what the suite uploads is
described by the code that made it - a flat colour for the eye, a sine tone
for the ear, and nothing else.

WHAT IT DEPENDS ON, and where that bites. It encodes with ffmpeg, which this
product ALREADY requires: apps/media_library/services.py shells out to it for
video thumbnails, for the composer's filmstrip and for trimming, so a
deployment that cannot run ffmpeg cannot run the media library either.

But "the developer's machine has ffmpeg" is not the same claim as "the CI
runner has ffmpeg", and the workflow installs exactly two things by hand -
PostgreSQL and Chromium. Nothing there puts an encoder on the runner, so a
helper that only ever calls `ffmpeg` would work here and skip, or fail,
there. It therefore takes the system binary when there is one and falls back
to the one imageio-ffmpeg ships inside the Python requirements, which travel
with the test job by definition.

Look at what it makes:

    python tests/e2e/make_test_video.py some-name.mp4
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

#: Big enough to be a real frame, small enough to encode in well under a
#: second. Both dimensions are even, which yuv420p requires.
WIDTH = 640
HEIGHT = 360

#: Long enough to have several keyframes' worth of timeline for anything that
#: seeks into it, short enough that a test never waits for it.
SECONDS = 3
FRAMES_PER_SECOND = 25

#: The same roast brown the still picture uses, so a person reading the
#: filmstrip can tell at a glance that both came from this suite.
COLOUR = "0x7A4F2E"

#: A 440 Hz sine - concert A. An audio track matters: a platform that rejects
#: silent or track-less video would otherwise be "tested" by a file that never
#: had the thing it objects to.
TONE_HZ = 440
SAMPLE_RATE = 44100


class NoEncoderError(RuntimeError):
    """Raised when no encoder can be found, naming what that costs."""


def find_ffmpeg() -> str:
    """The system's ffmpeg if it has one, otherwise the packaged one.

    The system binary is preferred because it is the one the APPLICATION will
    call in this same environment - if the media library is going to shell out
    to it, the harness had better be exercising the same build. The packaged
    fallback exists so a machine that installed the Python requirements and
    nothing else, which is what CI is, can still make the file.
    """
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
    except ImportError as missing:
        raise NoEncoderError(
            "no ffmpeg on PATH and imageio-ffmpeg is not installed, so no "
            "video can be made and the video-only platforms cannot be "
            "published to. This is the same ffmpeg the media library shells "
            "out to for thumbnails and filmstrips."
        ) from missing
    return imageio_ffmpeg.get_ffmpeg_exe()


def make_test_video(path: str | Path) -> str:
    """Encode the test video at `path` and return the path as a string.

    Overwrites whatever is there. Nothing caches: an artefact that survives
    between runs is one that can make a fixed defect look unfixed, which has
    already happened once in this suite with the still image.
    """
    ffmpeg = find_ffmpeg()

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # lavfi generates both inputs, so no source file is needed for either.
    # -shortest ends the file when the shorter input does, which keeps the
    # two tracks the same length instead of leaving a tail of silence.
    finished = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={COLOUR}:s={WIDTH}x{HEIGHT}:r={FRAMES_PER_SECOND}:d={SECONDS}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={TONE_HZ}:sample_rate={SAMPLE_RATE}:duration={SECONDS}",
            "-c:v",
            "libx264",
            # yuv420p and the +faststart flag are what makes the result play
            # in a browser at all; without them the composer's own preview
            # would be testing a file no viewer accepts.
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        raise NoEncoderError(f"ffmpeg refused to build the test video ({finished.returncode}):\n{finished.stderr}")

    # A file that exists but is empty is the failure this suite has already
    # been fooled by once, so its size is checked rather than assumed.
    if not path.is_file() or path.stat().st_size == 0:
        raise NoEncoderError(f"ffmpeg reported success but {path} is empty")

    return str(path)


if __name__ == "__main__":
    import sys

    where = sys.argv[1] if len(sys.argv) > 1 else "test-video.mp4"
    made = make_test_video(where)
    print(f"{made} — {Path(made).stat().st_size} bytes")
