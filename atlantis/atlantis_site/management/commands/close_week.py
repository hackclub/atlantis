"""Settle every challenge week that has finished, and tell people who's out.

Nothing on the site waits for this: a page works out where someone stands from
their timelapses and savers whenever it is asked, so a week is judged correctly
whether or not this has run. What the command adds is the two things a lazy
read cannot do — write down what the week looked like at the moment it closed,
and send the one DM telling someone they have been dropped.

Safe to run late, twice, or every hour. A week is only settled once (the
WeekOutcome row is the claim) and a DM only goes out once (notified_at is the
marker), so the usual cron is:

    0 * * * *  python manage.py close_week

Hourly rather than weekly on purpose: the deadline is local midnight Eastern
and the host's clock is UTC, so an hourly run settles every week within the
hour of it ending without anything having to agree about zones.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ... import challenge, weeks
from ...models import Profile, WeekOutcome


class Command(BaseCommand):
    help = "Record the outcome of every finished challenge week and DM anyone dropped."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be written and sent, and write nothing.",
        )
        parser.add_argument(
            "--no-notify",
            action="store_true",
            help="Settle the weeks but send no Slack DMs.",
        )
        parser.add_argument(
            "--week",
            type=int,
            default=None,
            help="Settle only this week (it still has to have finished).",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        dry = options["dry_run"]
        notify = not options["no_notify"]

        closed = weeks.closed_weeks(now)
        if options["week"] is not None:
            if options["week"] not in closed:
                self.stdout.write(
                    self.style.WARNING(f"Week {options['week']} hasn't finished yet.")
                )
                return
            closed = [options["week"]]

        if not closed:
            self.stdout.write("No week has finished yet.")
            return

        # Only people who have a profile — the accounts that came through the
        # HCA login and are actually in the program.
        profiles = Profile.objects.select_related("user")
        settled = notified = 0

        for profile in profiles:
            user = profile.user
            state = challenge.standing(user, now)
            existing = {
                row.week_index: row
                for row in WeekOutcome.objects.filter(user=user)
            }

            for index in closed:
                week = state.weeks[index - 1]
                # Week 1 short is still live until week 2 closes (the grace
                # pair), so there's nothing to write down about it yet.
                if week.pending:
                    continue
                row = existing.get(index)
                if row is None:
                    settled += 1
                    if not dry:
                        row = self._settle(user, week)
                    else:
                        self.stdout.write(
                            f"  would settle {user.username} week {index}: "
                            f"{week.credited_minutes}m, {'met' if week.met else 'MISSED'}"
                        )
                        continue

                # The DM goes to whoever is out *now*, which is not the same as
                # whoever missed this week: someone who missed week 3 and bought
                # their way back in should not be told they are out, and the
                # marker means they are never told twice.
                if week.missed and row.notified_at is None and state.eliminated:
                    notified += 1
                    if not dry:
                        self._notify(profile, state, row)
                    else:
                        self.stdout.write(f"  would DM {user.username} about week {index}")

        verb = "Would settle" if dry else "Settled"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {settled} week outcome(s) across weeks {closed}; "
            f"{'would send' if dry else 'sent'} {notified} DM(s)."
        ))

    @transaction.atomic
    def _settle(self, user, week):
        """Write down how a week ended. get_or_create is the idempotency."""
        row, _ = WeekOutcome.objects.get_or_create(
            user=user,
            week_index=week.index,
            defaults={
                "real_minutes": week.tracked_minutes,
                "passed": week.met,
            },
        )
        return row

    def _notify(self, profile, state, row):
        """One DM per missed week, to someone who is still out because of it."""
        # Imported here rather than at module scope so the Slack client isn't
        # built for a --dry-run that will never send anything.
        from ...views.helpers import send_slack_dm

        hours = state.saver_hours_needed
        week = state.weeks[row.week_index - 1]
        sent = False

        # The grace pair missed together, so it gets one DM, not one per week.
        if week.grace:
            pair = WeekOutcome.objects.filter(
                user=profile.user, week_index__in=weeks.grace_weeks(),
            )
            if pair.filter(notified_at__isnull=False).exists():
                row.notified_at = timezone.now()
                row.save(update_fields=["notified_at"])
                return False
            missed = (
                f"{weeks.grace_minutes() // 60} hours across {week.grace_label}"
            )
        else:
            missed = f"{weeks.WEEKLY_HOURS} hours for week {row.week_index}"

        if profile.slack_id:
            sent = send_slack_dm(
                f"You didn't hit {missed}, "
                "so you're out of Atlantis for now. "
                f"Buying {hours} missed-week streak saver{'s' if hours != 1 else ''} "
                "in the shop puts you back in: https://atlantis.hackclub.com/shop/ "
                "You can still make projects, journal and ship while you're out, "
                "so if you're short on pearls, your ships can earn them.",
                profile.slack_id,
            )

        # Marked either way. A user with no Slack id has nowhere to be told, and
        # leaving it null would have every future run try again forever.
        row.notified_at = timezone.now()
        row.save(update_fields=["notified_at"])
        return sent
