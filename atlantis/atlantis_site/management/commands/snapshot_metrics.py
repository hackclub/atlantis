"""Save the metrics page as it reads at 23:59, Eastern.

The page is almost all windows cut back from now, so yesterday's numbers are
gone by morning unless something wrote them down. This does, once a day; the
page's day picker reads them back.

scheduler.sh runs it from a loop of its own that sleeps until 23:59 in
CHALLENGE_TIMEZONE, so the host's clock being UTC and DST moving the offset
don't enter into it. The command still checks the clock itself so that a run
at any other time — by hand, or one held up past midnight — doesn't file a
half-finished day, or the first minutes of the next one, under a date. Pass
--force to take one anyway. A rerun for the same day replaces it.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from ... import weeks
from ...views.admin.metrics import take_snapshot

# The local minute a snapshot is taken in: the last one of the day.
SNAPSHOT_AT = (23, 59)


class Command(BaseCommand):
    help = "Save today's metrics page (run at 23:59 Eastern)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Snapshot now, whatever the local time.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        local = timezone.localtime(now, weeks.zone())
        if (local.hour, local.minute) != SNAPSHOT_AT and not options["force"]:
            self.stdout.write(
                f"It's {local:%H:%M} in {weeks.zone().key}; snapshots are only "
                f"taken at {SNAPSHOT_AT[0]}:{SNAPSHOT_AT[1]:02}. Skipping."
            )
            return

        snapshot = take_snapshot(now)
        self.stdout.write(self.style.SUCCESS(
            f"Saved metrics for {snapshot.day:%Y-%m-%d} at {local:%H:%M %Z}."
        ))
