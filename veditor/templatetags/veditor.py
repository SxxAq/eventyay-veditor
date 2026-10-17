"""Template tags and filters for VEditor video review integrations."""

from __future__ import annotations

import logging
from typing import Any

from django import template
from django.urls import reverse

logger = logging.getLogger(__name__)
register = template.Library()


def is_veditor_active(event: Any) -> bool:
    """Return True if the VEditor plugin is active and configured for the given event."""
    if not event:
        return False

    if hasattr(event, "get_plugins"):
        try:
            plugins = event.get_plugins()
            if "veditor" in plugins or "eventyay_veditor" in plugins or any("veditor" in str(p) for p in plugins):
                return True
        except Exception as exc:
            logger.debug("Failed checking event plugins via get_plugins(): %s", exc)

    plugins_attr = getattr(event, "plugins", None)
    if isinstance(plugins_attr, str) and "veditor" in plugins_attr:
        return True
    if isinstance(plugins_attr, (list, set, tuple)) and any("veditor" in str(p) for p in plugins_attr):
        return True

    # Fallback: check if event has veditor settings explicitly configured
    if hasattr(event, "settings"):
        try:
            if event.settings.get("veditor_api_base_url") or event.settings.get("veditor_api_key"):
                return True
        except Exception:
            pass

    return False


@register.filter(name="can_review_video")
def can_review_video(submission: Any, user: Any = None) -> bool:
    """Return True if the submission is confirmed, has VEditor enabled, and user is authorized."""
    if not submission:
        return False

    # 1. Strict State Filtering: Only confirmed sessions can launch into video review
    state = getattr(submission, "state", None)
    if str(state).lower() != "confirmed":
        return False

    event = getattr(submission, "event", None)
    if not event:
        return False

    # 2. Event & Plugin Scoping
    if not is_veditor_active(event):
        return False

    # 3. Ownership / Authorization Check (if user is provided)
    if user is not None and getattr(user, "is_authenticated", False):
        user_email = (getattr(user, "email", None) or "").strip().lower()
        is_speaker = False

        speakers_attr = getattr(submission, "speakers", None)
        if user_email and speakers_attr:
            speakers = speakers_attr.all() if callable(getattr(speakers_attr, "all", None)) else speakers_attr
            for sp in speakers:
                sp_email = (getattr(sp, "email", None) or "").strip().lower()
                if sp_email and sp_email == user_email:
                    is_speaker = True
                    break

        has_perm = hasattr(user, "has_event_permission") and user.has_event_permission(
            getattr(event, "organizer", None),
            event,
            "can_change_event_settings",
        )

        if not is_speaker and not has_perm:
            return False

    return True


@register.simple_tag(name="speaker_review_url")
def speaker_review_url(submission: Any) -> str:
    """Return the direct 1-click video review launch URL for a confirmed submission."""
    if not submission:
        return ""

    event = getattr(submission, "event", None)
    if not event:
        return ""

    organizer = getattr(event, "organizer", None)
    org_slug = getattr(organizer, "slug", None)
    event_slug = getattr(event, "slug", None)
    code = getattr(submission, "code", None)

    if not (org_slug and event_slug and code):
        return ""

    try:
        return reverse(
            "plugins:veditor:speaker-review",
            kwargs={"organizer": org_slug, "event": event_slug, "talk_code": code},
        )
    except Exception:
        return f"/common/event/{org_slug}/{event_slug}/veditor/talk/{code}/review/"


@register.filter(name="speaker_review_url")
def speaker_review_url_filter(submission: Any) -> str:
    """Filter alias for speaker_review_url."""
    return speaker_review_url(submission)
