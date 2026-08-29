"""Inactivity detection over a compiled Lookout video.

A timelapse is sixty times faster than the session it was stitched from, so a
reviewer scrubbing one is looking for the stretches where nothing happens —
the screen the shipper walked away from, the tutorial they left playing, the
half hour of an idle editor. Finding those by eye is the slowest part of the
job and the easiest part to get wrong.

So we find them first. One ffmpeg pass samples the video at 1fps, subtracts
each frame from the one before it, and asks `blackframe` which of those
difference frames are essentially black — which is to say, which seconds of
video show no change at all. Consecutive dark frames are collapsed into
segments, and the short ones are dropped.

Nothing here decides anything. The segments are drawn under the player as a
second track so the reviewer knows where to look; every deduction is still a
range a person typed, with a reason attached. A checker that quietly removed
time would be a checker nobody could argue with.

Units: the sample rate is one frame per video second, and Lookout stitches one
recorded minute into one second of video, so one segment second is one tracked
minute. Segment offsets are in the video's own timeline — the same timeline
the reviewer's ranges are read in.
"""

import logging
import os
import re
import subprocess
import tempfile

import requests

logger = logging.getLogger(__name__)

# blackframe's two knobs. A difference frame counts as "inactive" when at
# least AMOUNT percent of its pixels are darker than THRESHOLD. The threshold
# is well above zero on purpose: compression noise moves pixels a little even
# on a frame where nothing happened, and a stricter setting finds no idle time
# at all on a video that has plenty.
BLACKFRAME_AMOUNT = 98
BLACKFRAME_THRESHOLD = 25

# Frames sampled per second of video. One, because one video second is already
# one recorded minute and nothing finer means anything.
SAMPLE_FPS = 1

# Shortest run worth reporting, in video seconds (= recorded minutes). A
# single idle minute is normal; two in a row is a gap.
MIN_INACTIVE_SECONDS = 2

# Long enough for a session of any length, short enough that a wedged ffmpeg
# doesn't hold a worker forever.
FFMPEG_TIMEOUT = 20 * 60
DOWNLOAD_TIMEOUT = 120
# Compiled lapses are small (a minute of video per hour recorded), so anything
# past this is not one, and streaming it to disk is not free.
MAX_VIDEO_BYTES = 512 * 1024 * 1024

# Shift the sampled stream one frame and subtract it from itself: what is left
# is how much changed between each pair of consecutive frames. `eof_action`
# stops at the shorter input, which is the shifted one.
FILTER_COMPLEX = (
    "[0:v]fps={fps},format=gray,split[a][b];"
    "[a]trim=start_frame=1,setpts=PTS-STARTPTS[shifted];"
    "[b][shifted]blend=all_mode=difference:eof_action=endall,"
    "blackframe=amount={amount}:threshold={threshold}"
).format(fps=SAMPLE_FPS, amount=BLACKFRAME_AMOUNT, threshold=BLACKFRAME_THRESHOLD)

_BLACKFRAME_RE = re.compile(r"\[Parsed_blackframe.*?\]\s*frame:(\d+)\s+pblack:(\d+)")
_PROGRESS_RE = re.compile(r"frame=\s*(\d+)")


class ActivityCheckError(Exception):
    """The check could not be run. The message says why, for the log."""


def empty_result():
    return {
        "inactive_frames": 0,
        "total_frames": 0,
        "inactive_percentage": 0.0,
        "segments": [],
    }


def ffmpeg_available():
    """Whether ffmpeg is on PATH. Checked before a batch, not per session."""
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def download_video(url, timeout=DOWNLOAD_TIMEOUT):
    """Stream a compiled lapse to a temp file. Returns the path, or None.

    Streamed rather than read into memory, and capped: the file is only ever
    handed to ffmpeg, and a video that doesn't fit the cap isn't one of ours.
    """
    handle, path = tempfile.mkstemp(prefix="lapse_", suffix=".mp4")
    written = 0
    try:
        with requests.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with os.fdopen(handle, "wb") as out:
                handle = None
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_VIDEO_BYTES:
                        raise ActivityCheckError(
                            f"{url} is larger than {MAX_VIDEO_BYTES} bytes"
                        )
                    out.write(chunk)
    except (requests.RequestException, ActivityCheckError, OSError) as exc:
        if handle is not None:
            os.close(handle)
        _discard(path)
        logger.warning("Activity check could not fetch %s: %s", url, exc)
        return None

    if not written:
        _discard(path)
        logger.warning("Activity check fetched an empty video from %s", url)
        return None
    return path


def _discard(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def run_ffmpeg(path):
    """The single analysis pass. Returns ffmpeg's log, or None if it failed."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-i", path,
                "-filter_complex", FILTER_COMPLEX,
                "-f", "null", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=FFMPEG_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Activity check ffmpeg failed on %s: %s", path, exc)
        return None

    output = result.stdout.decode("utf-8", "replace")
    if result.returncode != 0:
        logger.warning(
            "Activity check ffmpeg exited %s on %s: %s",
            result.returncode, path, output[-500:],
        )
        return None
    return output


def parse_output(output):
    """(total difference frames, indices of the inactive ones).

    blackframe only reports the frames it considers black, so the denominator
    comes from ffmpeg's own progress counter rather than from the matches.
    """
    inactive = [int(frame) for frame, _ in _BLACKFRAME_RE.findall(output)]
    totals = [int(n) for n in _PROGRESS_RE.findall(output)]
    return (max(totals) if totals else 0), inactive


def collapse_into_segments(indices):
    """Runs of consecutive frame indices, as {start, end, duration} seconds.

    Half-open at the end, like every other range in the review: a single
    inactive frame at index 5 is 5-6, one second long.
    """
    if not indices:
        return []

    ordered = sorted(set(indices))
    segments = []
    start = previous = ordered[0]
    for index in ordered[1:]:
        if index == previous + 1:
            previous = index
            continue
        segments.append(_segment(start, previous))
        start = previous = index
    segments.append(_segment(start, previous))
    return segments


def _segment(start, last):
    end = last + 1
    return {"start": start, "end": end, "duration": end - start}


def analyse_file(path):
    """Run the whole check over a local video. Never raises."""
    output = run_ffmpeg(path)
    if output is None:
        return empty_result()

    total_frames, inactive = parse_output(output)
    if total_frames < 1:
        return empty_result()

    segments = [
        segment for segment in collapse_into_segments(inactive)
        if segment["duration"] >= MIN_INACTIVE_SECONDS
    ]
    inactive_frames = sum(segment["duration"] for segment in segments)

    return {
        "inactive_frames": inactive_frames,
        "total_frames": total_frames + 1,
        "inactive_percentage": round(inactive_frames / total_frames * 100, 1),
        "segments": segments,
    }


def check_session(session):
    """Analyse one LookoutSession's compiled video. Never raises.

    Returns the result dict; an empty one means the video couldn't be read,
    which is reported the same way as "nothing found" would be to the caller
    but is not written to the session by check_and_store.
    """
    path = download_video(session.video_url)
    if path is None:
        return None
    try:
        return analyse_file(path)
    finally:
        _discard(path)


def check_and_store(session):
    """Analyse a session and record what was found. Returns the result or None.

    None means the check did not run — the video was unreachable, or ffmpeg
    could not read it. Nothing is stored in that case, so the session stays
    unchecked and the next run picks it up again, rather than being marked
    clean by a failure.
    """
    result = check_session(session)
    if result is None:
        return None

    from django.utils import timezone

    session.inactive_frame_count = result["inactive_frames"]
    session.inactive_percentage = result["inactive_percentage"]
    session.inactive_segments = result["segments"]
    session.activity_checked_at = timezone.now()
    session.save(update_fields=[
        "inactive_frame_count",
        "inactive_percentage",
        "inactive_segments",
        "activity_checked_at",
        "updated_at",
    ])
    return result
