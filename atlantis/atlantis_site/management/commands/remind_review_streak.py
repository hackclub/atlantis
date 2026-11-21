"""At 11pm their time, DM any reviewer whose daily streak is about to end.

A reviewer's streak (daily_streak.py) survives a day only if they make
DAILY_GOAL project reviews before their own midnight. Whoever has a run going
and is still short when their clock reaches REMIND_HOUR gets one DM saying how
many reviews are left.

Nobody without a run going is told anything. A nightly "you didn't review
today" to everyone with the permission would be nagging. The DM is there to
save something they already built.

Safe to run as often as you like. Outside a reviewer's 11pm hour it does
nothing for them, and a ReviewStreakReminder row (keyed on their local date)
means each person hears once a day however many times it runs in that hour.
The scheduler runs it every few minutes, so the DM goes out a little after
eleven in every zone, and one missed run is made up by the next.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction
from django.utils import timezone

from ...daily_streak import (
    REMIND_HOUR, can_keep_streak, daily_streak, ensure_zone,
)
from ...models import T1, T2, T3, Profile, ReviewStreakReminder

# Reviewed this recently, or they can't have a run going. A run needs a full
# day on the reviewer's local yesterday or today, and yesterday began less
# than 48 hours ago in any zone. The margin is for luck.
LOOKBACK = timedelta(days=3)


class Command(BaseCommand):
    help = "At 11pm local time, DM reviewers short of today's reviews that their streak is about to end."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report who would be DMed, and send and write nothing.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        dry = options["dry_run"]

        since = now - LOOKBACK
        recent = set()
        for model in (T1, T2, T3):
            recent.update(
                model.objects.filter(reviewed_at__gte=since)
                .values_list("reviewer_id", flat=True)
                .distinct()
            )

        profiles = (
            Profile.objects.filter(user_id__in=recent, banned=False)
            .exclude(slack_id="")
            .select_related("user")
        )
        sent = 0
        for profile in profiles:
            zone = ensure_zone(profile, save=not dry)
            local = now.astimezone(zone)
            if local.hour != REMIND_HOUR:
                continue
            user = profile.user
            if ReviewStreakReminder.objects.filter(user=user, day=local.date()).exists():
                continue
            if not can_keep_streak(user):
                continue

            streak = daily_streak(user, now, zone)
            if not streak.at_risk:
                continue

            sent += 1
            if dry:
                self.stdout.write(
                    f"  would DM {user.username}: {streak.days}-day streak, "
                    f"{streak.left} left ({zone})"
                )
                continue
            self._remind(profile, streak)

        verb = "Would send" if dry else "Sent"
        self.stdout.write(self.style.SUCCESS(f"{verb} {sent} review streak reminder(s)."))

    def _remind(self, profile, streak):
        """Claim the reminder, then send it, so a crash can't send it twice."""
        # Imported here so a --dry-run never builds the Slack client.
        from ...views.helpers import send_slack_dm

        try:
            with transaction.atomic():
                ReviewStreakReminder.objects.create(user=profile.user, day=streak.date)
        except IntegrityError:
            # Another run got there first.
            return False

        days = f"{streak.days}-day"
        reviews = "review" if streak.left == 1 else "reviews"
        return send_slack_dm(
            f":fire: Your {days} review streak ends at midnight! You've done "
            f"{streak.today} of today's {streak.goal} reviews, so *{streak.left} more "
            f"{reviews}* will keep it going: https://atlantis.hackclub.com/root/",
            profile.slack_id,
        )
