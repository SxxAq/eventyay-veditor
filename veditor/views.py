"""Views for the VEditor Eventyay plugin."""

from __future__ import annotations

import os
from typing import Any

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from django_scopes import scopes_disabled
from eventyay.base.models import TalkSlot
from eventyay.control.permissions import EventPermissionRequiredMixin

from .client import VEditorClient
from .exceptions import VEditorConfigError, VEditorError
from .forms import VEditorSettingsForm


class ConnectView(EventPermissionRequiredMixin, TemplateView):
    """View to view event sync status and launch VEditor with single-click SSO."""

    permission = "can_change_event_settings"
    template_name = "veditor/connect.html"

    def get_talk_slots(self) -> list[TalkSlot]:
        """Fetch all scheduled and confirmed talk slots for the current active schedule."""
        with scopes_disabled():
            event = self.request.event
            schedule = getattr(event, "current_schedule", None) or getattr(event, "wip_schedule", None)

            if schedule:
                if hasattr(schedule, "scheduled_talks"):
                    slots = list(schedule.scheduled_talks)
                else:
                    slots = list(schedule.talks.filter(submission__isnull=False).select_related("submission", "room").order_by("start"))
            else:
                slots = list(
                    TalkSlot.objects.filter(
                        schedule__event=event,
                        submission__isnull=False,
                    )
                    .select_related("submission", "room")
                    .order_by("start")
                )

            # Deduplicate by submission_id to avoid multiples across schedule revisions
            seen_submissions = set()
            unique_slots = []
            for slot in slots:
                sub_id = getattr(slot, "submission_id", None) or getattr(slot, "id", None)
                if sub_id not in seen_submissions:
                    seen_submissions.add(sub_id)
                    unique_slots.append(slot)

            return unique_slots

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Populate template context with event details, talk counts, and settings form."""
        context = super().get_context_data(**kwargs)
        event = self.request.event
        talk_slots = self.get_talk_slots()

        context["event"] = event
        context["talks"] = talk_slots
        context["talks_count"] = len(talk_slots)

        # Pre-populate form with saved event settings or platform environment defaults
        saved_url = (
            (event.settings.get("veditor_api_base_url") if hasattr(event, "settings") else None)
            or os.environ.get("VEDITOR_API_BASE_URL")
            or "http://localhost:8080"
        )
        saved_key = (event.settings.get("veditor_api_key") if hasattr(event, "settings") else None) or os.environ.get("VEDITOR_API_KEY") or ""
        saved_event_id = event.settings.get("veditor_event_id") if hasattr(event, "settings") else None

        if "form" not in context:
            context["form"] = VEditorSettingsForm(
                initial={
                    "veditor_api_base_url": saved_url,
                    "veditor_api_key": saved_key,
                    "veditor_event_id": saved_event_id or "",
                }
            )

        try:
            client = VEditorClient(event=event)
            context["veditor_configured"] = True
            context["veditor_base_url"] = client.base_url
            raw_event_id = event.settings.get("veditor_event_id") if hasattr(event, "settings") else None
            context["veditor_target_event_id"] = int(raw_event_id or event.id)
        except VEditorConfigError as exc:
            context["veditor_configured"] = False
            context["config_error"] = str(exc)

        return context

    def post(self, request, *args, **kwargs):
        """Handle saving settings or executing talk synchronization and redirect."""
        event = request.event
        action = request.POST.get("action")

        if action == "save_settings":
            form = VEditorSettingsForm(request.POST)
            if form.is_valid():
                if hasattr(event, "settings"):
                    event.settings.set(
                        "veditor_api_base_url",
                        form.cleaned_data["veditor_api_base_url"].rstrip("/"),
                    )
                    event.settings.set(
                        "veditor_api_key",
                        form.cleaned_data["veditor_api_key"].strip(),
                    )
                    event_id_val = form.cleaned_data.get("veditor_event_id")
                    if event_id_val is not None:
                        event.settings.set("veditor_event_id", str(event_id_val))
                    else:
                        event.settings.set("veditor_event_id", "")

                messages.success(request, _("VEditor connection settings saved successfully."))
                return redirect(
                    reverse(
                        "plugins:veditor:connect",
                        kwargs={"organizer": event.organizer.slug, "event": event.slug},
                    )
                )
            else:
                context = self.get_context_data(**kwargs)
                context["form"] = form
                return self.render_to_response(context)

        # Default action: sync talks and launch VEditor
        talk_slots = self.get_talk_slots()
        raw_event_id = event.settings.get("veditor_event_id") if hasattr(event, "settings") else None
        target_event_id = int(raw_event_id or event.id)

        try:
            client = VEditorClient(event=event)
            # 1. Atomic bulk synchronization of talks
            client.sync_talks(event_id=target_event_id, talk_slots=talk_slots)

            # 2. Request scoped SSO JWT for organizer, falling back to direct studio link if endpoint is 404
            try:
                token = client.request_sso_jwt(event_id=target_event_id, role="organiser")
                redirect_url = f"{client.base_url}/studio?event_id={target_event_id}&sso_token={token}"
            except VEditorError as sso_exc:
                if sso_exc.status_code == 404:
                    redirect_url = f"{client.base_url}/studio?api_key={client.api_key}&event_id={target_event_id}"
                else:
                    raise

            # 3. Redirect browser to VEditor
            return HttpResponseRedirect(redirect_url)

        except (VEditorError, ValueError) as exc:
            messages.error(
                request,
                _("Failed to synchronize talks with VEditor: {error}").format(error=str(exc)),
            )
            return redirect(
                reverse(
                    "plugins:veditor:connect",
                    kwargs={
                        "organizer": event.organizer.slug,
                        "event": event.slug,
                    },
                )
            )
