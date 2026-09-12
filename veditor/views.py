"""Views for the VEditor Eventyay plugin."""

from __future__ import annotations

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


class ConnectView(EventPermissionRequiredMixin, TemplateView):
    """View to view event sync status and launch VEditor with single-click SSO."""

    permission = "can_change_event_settings"
    template_name = "veditor/connect.html"

    def get_talk_slots(self) -> list[TalkSlot]:
        """Fetch all scheduled and confirmed talk slots for the current event."""
        with scopes_disabled():
            return list(
                TalkSlot.objects.filter(
                    schedule__event=self.request.event,
                    submission__isnull=False,
                )
                .select_related("submission", "room")
                .order_by("start")
            )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Populate template context with event details and talk counts."""
        context = super().get_context_data(**kwargs)
        event = self.request.event
        talk_slots = self.get_talk_slots()

        context["event"] = event
        context["talks"] = talk_slots
        context["talks_count"] = len(talk_slots)

        try:
            client = VEditorClient()
            context["veditor_configured"] = True
            context["veditor_base_url"] = client.base_url
        except VEditorConfigError as exc:
            context["veditor_configured"] = False
            context["config_error"] = str(exc)

        return context

    def post(self, request, *args, **kwargs):
        """Execute atomic talk synchronization and redirect organizer to VEditor."""
        event = request.event
        talk_slots = self.get_talk_slots()

        try:
            client = VEditorClient()
            # 1. Atomic bulk synchronization of talks
            client.sync_talks(event_id=event.id, talk_slots=talk_slots)

            # 2. Request scoped SSO JWT for organizer
            token = client.request_sso_jwt(event_id=event.id, role="organiser")

            # 3. Redirect browser to VEditor
            redirect_url = f"{client.base_url}/?sso_token={token}"
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
