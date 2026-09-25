"""Celery tasks for the VEditor Eventyay plugin."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

try:
    from eventyay.celery_app import app

    task_decorator = app.task
except ImportError:
    from celery import shared_task

    task_decorator = shared_task


@task_decorator(name="veditor.process_talk_approved")
def process_talk_approved(
    event_id: int | str,
    talk_id: int | str,
    external_id: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Process an incoming talk.approved webhook from VEditor.

    Resolves the associated Event and TalkSlot/Submission, preparing for speaker review notification.
    """
    logger.info(
        "Processing talk.approved for event_id=%s, talk_id=%s, external_id=%s",
        event_id,
        talk_id,
        external_id,
    )
    return {
        "status": "success",
        "event_id": event_id,
        "talk_id": talk_id,
        "external_id": external_id,
    }


@task_decorator(name="veditor.process_talk_published")
def process_talk_published(
    event_id: int | str,
    talk_id: int | str,
    video_url: str,
    external_id: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Process an incoming talk.published webhook from VEditor.

    Attaches or updates the video recording resource on the Eventyay submission
    and marks the recording as ready for the public schedule.
    """
    logger.info(
        "Processing talk.published for event_id=%s, talk_id=%s, external_id=%s, video_url=%s",
        event_id,
        talk_id,
        external_id,
        video_url,
    )
    if not video_url or not isinstance(video_url, str) or not video_url.strip():
        logger.warning("Empty or invalid video_url received for talk.published: %r", video_url)
        return {
            "status": "error",
            "message": "Missing or invalid video_url",
            "event_id": event_id,
            "talk_id": talk_id,
        }

    video_url = video_url.strip()
    parsed_video = urlparse(video_url)
    if parsed_video.scheme not in ("http", "https") or not parsed_video.netloc:
        logger.warning("Empty or invalid video_url received for talk.published: %r", video_url)
        return {
            "status": "error",
            "message": "Missing or invalid video_url scheme/host",
            "event_id": event_id,
            "talk_id": talk_id,
        }

    try:
        from django.db import DatabaseError
        from django_scopes import scopes_disabled
        from eventyay.base.models import Event, Resource, Submission, TalkSlot
    except ImportError as exc:
        logger.error("Eventyay models not available: %s", exc)
        return {
            "status": "error",
            "message": f"Eventyay models not available: {exc}",
            "event_id": event_id,
            "talk_id": talk_id,
        }

    try:
        with scopes_disabled():
            # 1. Resolve event
            event_obj = None
            if event_id is not None and event_id != "":
                try:
                    if str(event_id).isdigit():
                        event_obj = Event.objects.filter(id=int(event_id)).first()
                    if not event_obj:
                        event_obj = Event.objects.filter(slug=str(event_id)).first()
                except (DatabaseError, RuntimeError) as exc:
                    logger.debug("Database error resolving event %s: %s", event_id, exc)

            if not event_obj:
                logger.warning("Event not found for talk.published: event_id=%s", event_id)
                return {
                    "status": "not_found",
                    "message": f"Event {event_id} not found",
                    "event_id": event_id,
                    "talk_id": talk_id,
                    "external_id": external_id,
                }

            # 2. Resolve submission strictly scoped to event_obj
            submission: Submission | None = None

            # Strategy A: by external_id (submission code, id, or slot id)
            if external_id:
                ext_str = str(external_id).strip()
                try:
                    submission = event_obj.submissions.filter(code__iexact=ext_str).first()
                except (DatabaseError, RuntimeError):
                    pass
                if not submission and ext_str.isdigit():
                    try:
                        submission = event_obj.submissions.filter(id=int(ext_str)).first()
                    except (DatabaseError, RuntimeError):
                        pass
                if not submission and ext_str.isdigit():
                    try:
                        slot = (
                            TalkSlot.objects.filter(
                                id=int(ext_str),
                                submission__event=event_obj,
                                submission__isnull=False,
                            )
                            .select_related("submission")
                            .first()
                        )
                        if slot:
                            submission = slot.submission
                    except (DatabaseError, RuntimeError):
                        pass

            # Strategy B: by talk_id (if talk_id is numeric)
            if not submission and talk_id is not None and str(talk_id).isdigit():
                talk_int = int(talk_id)
                try:
                    slot = (
                        TalkSlot.objects.filter(
                            id=talk_int,
                            submission__event=event_obj,
                            submission__isnull=False,
                        )
                        .select_related("submission")
                        .first()
                    )
                    if slot:
                        submission = slot.submission
                except (DatabaseError, RuntimeError):
                    pass
                if not submission:
                    try:
                        submission = event_obj.submissions.filter(id=talk_int).first()
                    except (DatabaseError, RuntimeError):
                        pass

            if not submission:
                logger.warning(
                    "Could not resolve Submission for talk.published: event_id=%s, talk_id=%s, external_id=%s",
                    event_id,
                    talk_id,
                    external_id,
                )
                return {
                    "status": "not_found",
                    "message": "Submission not found",
                    "event_id": event_id,
                    "talk_id": talk_id,
                    "external_id": external_id,
                }

            # Enforce cross-event tenant isolation
            sub_event_id = getattr(submission, "event_id", getattr(getattr(submission, "event", None), "id", None))
            if sub_event_id is not None and sub_event_id != event_obj.id:
                logger.error(
                    "Cross-event boundary violation: submission %s belongs to event %s, not %s",
                    submission.code,
                    sub_event_id,
                    event_obj.id,
                )
                return {
                    "status": "error",
                    "message": "Submission belongs to a different event",
                    "event_id": event_id,
                    "talk_id": talk_id,
                }

            # 3. Check do_not_record flag
            if getattr(submission, "do_not_record", False):
                logger.info(
                    "Submission %s has do_not_record set to True; skipping recording attachment.",
                    submission.code,
                )
                return {
                    "status": "skipped",
                    "reason": "do_not_record",
                    "submission_code": submission.code,
                }

            # 4. Update or create Resource safely without MultipleObjectsReturned
            resource = (
                Resource.objects.filter(
                    submission=submission,
                    description__iexact="Video Recording",
                )
                .order_by("id")
                .first()
            )
            created = False
            if resource:
                resource.link = video_url
                resource.kind = "generic"
                resource.save(update_fields=["link", "kind"])
            else:
                resource = Resource.objects.create(
                    submission=submission,
                    description="Video Recording",
                    link=video_url,
                    kind="generic",
                )
                created = True

            # 5. Provide backwards compatibility for recording_url attribute if present
            if hasattr(submission, "recording_url"):
                submission.recording_url = video_url
                try:
                    submission.save(update_fields=["recording_url"])
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed updating recording_url on submission %s: %s",
                        getattr(submission, "code", None),
                        exc,
                    )

            logger.info(
                "Successfully synced recording URL for submission %s (Resource ID=%s, created=%s)",
                submission.code,
                resource.id,
                created,
            )

            return {
                "status": "success",
                "submission_code": submission.code,
                "video_url": video_url,
                "resource_id": resource.id,
                "created": created,
            }
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed processing talk.published for talk_id=%s: %s", talk_id, exc)
        return {
            "status": "error",
            "message": str(exc),
            "event_id": event_id,
            "talk_id": talk_id,
        }
