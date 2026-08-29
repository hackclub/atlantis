"""Mock Lookout media server.

The real Lookout service (lookout.hackclub.com) won't be reachable from this
dev environment, so review videos/thumbnails are generated on the fly with
ffmpeg (sourced from the `imageio-ffmpeg` wheel) and cached under /tmp.
"""

import os
import subprocess
from pathlib import Path

from django.http import FileResponse, JsonResponse

from atlantis_site.models import LookoutSession

CACHE_DIR = Path("/tmp/mock-lookout")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

try:
    import imageio_ffmpeg

    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")


def _run(args):
    subprocess.run(args, check=True, capture_output=True)


def _video_path(session_id):
    session = LookoutSession.objects.filter(session_id=session_id).first()
    if not session:
        return None
    duration = max(1, (session.tracked_seconds or 60) // 60 + 1)  # 60× speed ➜ ≥1s of video
    path = CACHE_DIR / f"{session_id}.mp4"
    if not path.exists():
        _run(
            [
                FFMPEG, "-y",
                "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=12:d={duration}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest",
                str(path),
            ]
        )
    return path


def _thumb_path(session_id):
    session = LookoutSession.objects.filter(session_id=session_id).first()
    if not session:
        return None
    path = CACHE_DIR / f"{session_id}.jpg"
    if not path.exists():
        _run(
            [
                FFMPEG, "-y",
                "-f", "lavfi", "-i", "color=c=0x336699:s=640x360:d=1",
                "-frames:v", "1",
                str(path),
            ]
        )
    return path


def mock_root(request):
    return JsonResponse({"status": "mock-lookout-ok"})


def mock_video(request, session_id):
    path = _video_path(session_id)
    if path is None:
        return JsonResponse({"error": "not found"}, status=404)
    return FileResponse(path, content_type="video/mp4")


def mock_thumbnail(request, session_id):
    path = _thumb_path(session_id)
    if path is None:
        return JsonResponse({"error": "not found"}, status=404)
    return FileResponse(path, content_type="image/jpeg")