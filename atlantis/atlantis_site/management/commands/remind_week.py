"""DM everyone who's still short on Saturday evening, with how much is left.

A nudge, not a verdict: it goes to whoever is in the program and hasn't filled
this week's bar, a day and a bit before the week closes (weeks.reminder_at),
and says exactly how much time they still need to track.

Safe to run as often as you like. Before Saturday 6pm Eastern it does nothing,
and a WeekReminder row means each person hears once a week however many times
it runs after that — so the scheduler runs it every few minutes and it catches
up by itself if the container was down at six.

Week 1 gets no reminder: it's judged together with week 2 (weeks.GRACE_WEEKS),
so nothing is actually due when it ends. Week 2's reminder is about the pair's
ten hours.
"""

from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction
from django.utils import timezone

from ... import challenge, weeks
from ...models import Profile, WeekReminder


class Command(BaseCommand):
    help = "On Saturday evening, DM anyone short of this week's hours how much is left."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report who would be DMed, and send and write nothing.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        dry = options["dry_run"]

        index = weeks.current_week(now)
        if index is None:
            self.stdout.write("No week is running.")
            return
        if now < weeks.reminder_at(index):
            self.stdout.write(f"Week {index}'s reminder isn't due until {weeks.reminder_at(index)}.")
            return
        grace = weeks.grace_weeks()
        if index in grace and index != grace[-1]:
            self.stdout.write(f"Week {index} is judged with week {grace[-1]}; nothing is due yet.")
            return

        already = set(
            WeekReminder.objects.filter(week_index=index).values_list("user_id", flat=True)
        )
        sent = 0

        # Everyone who came through the HCA login and has somewhere to be told,
        # whether or not they've logged anything yet: the people with nothing
        # on the bar are exactly who a nudge is for.
        for profile in Profile.objects.select_related("user").exclude(slack_id=""):
            user = profile.user
            if user.id in already:
                continue

            state = challenge.standing(user, now)
            week = state.current
            # Whoever is out already got the elimination DM; a full bar needs
            # no nudge.
            if week is None or state.eliminated or week.done:
                continue

            sent += 1
            if dry:
                self.stdout.write(f"  would DM {user.username}: {week.left_display} left")
                continue
            self._remind(profile, week)

        verb = "Would send" if dry else "Sent"
        self.stdout.write(self.style.SUCCESS(f"{verb} {sent} week {index} reminder(s)."))

    def _remind(self, profile, week):
        """Claim the reminder, then send it — so a crash can't send it twice."""
        # Imported here rather than at module scope so the Slack client isn't
        # built for a --dry-run that will never send anything.
        from ...views.helpers import send_slack_dm

        try:
            with transaction.atomic():
                WeekReminder.objects.create(user=profile.user, week_index=week.index)
        except IntegrityError:
            # Another run got there first.
            return False

        if week.grace:
            goal = f"{week.required_display} across {week.grace_label}"
        else:
            goal = f"{week.required_display} this week"
        if week.bar_makeup_minutes:
            goal += f" ({week.bar_makeup_display} of it making up deflated time)"

        return send_slack_dm(
            f"Heads up: week {week.index} of Atlantis ends tomorrow (Sunday) at "
            f"11:59 PM Eastern. You've tracked {week.progress_display} of the {goal}, "
            f"so you have *{week.left_display}* left to track to stay in the program. "
            "Record it with Lapse and tape it into your project: "
            "https://atlantis.hackclub.com/dashboard/",
            profile.slack_id,
        )
