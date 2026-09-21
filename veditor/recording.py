"""Recording provider for the VEditor Eventyay plugin."""

from __future__ import annotations

import html
import logging
from urllib.parse import urlparse

try:
    from eventyay.agenda.recording import BaseRecordingProvider
except ImportError:

    class BaseRecordingProvider:  # type: ignore[no-redef]
        def __init__(self, event):
            self.event = event

        def get_recording(self, submission):
            raise NotImplementedError


logger = logging.getLogger(__name__)


class VEditorRecordingProvider(BaseRecordingProvider):
    """Provides recording iframe for talks with completed VEditor recordings."""

    def get_recording(self, submission) -> dict[str, str]:
        """Return iframe and CSP header for talk submission recording."""
        if getattr(submission, "do_not_record", False):
            return {}

        video_url = getattr(submission, "recording_url", None)

        if not video_url and hasattr(submission, "resources"):
            try:
                resource = (
                    submission.resources.filter(
                        description__iexact="Video Recording",
                    ).first()
                    or submission.resources.filter(
                        description__icontains="recording",
                    ).first()
                )
                if resource and resource.link:
                    video_url = resource.link
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Error looking up resources for submission %s: %s",
                    getattr(submission, "code", None),
                    exc,
                )

        if not video_url:
            return {}

        video_url = str(video_url).strip()
        escaped_url = html.escape(video_url)

        # Direct video files (e.g. mp4, webm, ogg, mov)
        clean_path = video_url.split("?")[0].lower()
        if any(clean_path.endswith(ext) for ext in (".mp4", ".webm", ".ogg", ".mov")):
            iframe = (
                f'<video controls class="w-100" style="max-width: 100%; border-radius: 4px;" src="{escaped_url}">'
                f'Your browser does not support the video tag. <a href="{escaped_url}">Watch video</a>'
                f"</video>"
            )
        else:
            iframe = (
                f'<div class="ratio ratio-16x9 veditor-recording-frame">'
                f'<iframe src="{escaped_url}" title="Talk Recording" allowfullscreen '
                f'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" '
                f'style="width: 100%; height: 100%; border: 0;"></iframe>'
                f"</div>"
            )

        parsed = urlparse(video_url)
        csp_header = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc and parsed.scheme else ""

        return {
            "iframe": iframe,
            "csp_header": csp_header,
        }
