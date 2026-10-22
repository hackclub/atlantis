"""Restore the week for everyone told the buffer had put them back in.

The "back in thanks to the buffer" export, pulled while a buffer was still
open, listed everyone short on the week rather than only those the buffer had
saved: a short week is pending until 2am, and pending read as saved (see
challenge.listed_while_buffering). Everyone on it was told they were back in.
This makes that true, with the same organizer pass the per-user page sets by
hand, so it shows on the roster and can be cleared there. A passed week also
sends its buffer forward, so time logged in it lands on the new week's bar.

Who was listed is rebuilt from the audit log: every export pulled inside a
buffer, as it stood at that moment. Only a week still missed is touched, so
anyone the buffer really did save, or who has bought it back since, is left
alone. Sends nothing.

    python manage.py honor_buffer_dm --by <organizer> --dry-run
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ... import challenge
from ...models import AuditLog, Profile, WeekOutcome

NOTE = "Told the buffer put them back in (export pulled before it closed)"


class Command(BaseCommand):
    help = "Pass the week for everyone the buffer export wrongly said was back in."

    def add_arguments(self, parser):
        parser.add_argument(
            "--by",
            required=True,
            help="Username of the organizer the passes are recorded under.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report who would be passed, and write nothing.",
        )

    def handle(self, *args, **options):
        organizer = get_user_model().objects.filter(username=options["by"]).first()
        if organizer is None:
            raise CommandError(f"No user named {options['by']!r}.")
        dry = options["dry_run"]

        # {user id: weeks they were told were saved}, across every export
        # pulled inside a buffer; one pulled after it closed was right.
        listed = {}
        exports = AuditLog.objects.filter(action="export_buffer_rescued").order_by("created_at")
        for export in exports:
            index, owners = challenge.listed_while_buffering(export.created_at)
            for owner in owners:
                listed.setdefault(owner, set()).add(index)
        if not listed:
            self.stdout.write("No export was pulled while a buffer was open; nothing to restore.")
            return

        # The export left the banned out, so they were never told.
        profiles = (
            Profile.objects.select_related("user")
            .filter(user_id__in=listed.keys(), banned=False)
            .order_by("user_id")
        )
        passed = 0

        for profile in profiles:
            user = profile.user
            state = challenge.standing(user)
            for index in sorted(listed[user.id]):
                week = state.weeks[index - 1]
                # The buffer really did save it, or they've bought it back since.
                if not week.missed:
                    continue
                if week.override == WeekOutcome.Override.FAIL:
                    self.stdout.write(f"  {user.username}: an organizer failed week {index}, left alone")
                    continue
                passed += 1
                verb = "would pass" if dry else "passed"
                self.stdout.write(f"  {verb} {user.username} week {index} ({week.credited_display})")
                if not dry:
                    self._pass(user, week, organizer)

        verb = "Would pass" if dry else "Passed"
        self.stdout.write(self.style.SUCCESS(f"{verb} {passed} week(s) across the {len(listed)} listed."))

    @transaction.atomic
    def _pass(self, user, week, organizer):
        outcome, _ = WeekOutcome.objects.get_or_create(
            user=user,
            week_index=week.index,
            defaults={"real_minutes": week.tracked_minutes, "passed": False},
        )
        outcome.override = WeekOutcome.Override.PASS
        outcome.override_by = organizer
        outcome.override_note = NOTE
        outcome.save(update_fields=["override", "override_by", "override_note"])
        # What override_week records, less the request a command doesn't have.
        AuditLog.objects.create(
            actor=organizer,
            action="override_week",
            target=f"{user.username} week {week.index}",
            metadata={
                "user_id": user.id,
                "week": week.index,
                "override": WeekOutcome.Override.PASS,
                "note": NOTE,
            },
        )
