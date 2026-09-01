"""Run inactivity detection over compiled Lapse footage.

Meant to be run on a timer, the way submit_airtable is. Every attached recording
gets one pass; one whose video couldn't be fetched or read is left
unchecked so the next run tries it again, rather than being recorded as clean.

    python manage.py check_timelapse_activity            # everything unchecked
    python manage.py check_timelapse_activity --limit 20
    python manage.py check_timelapse_activity --session 41 --force
    python manage.py check_timelapse_activity --project 7 --force
"""

from django.core.management.base import BaseCommand, CommandError

from ... import activity
from ...models import Timelapse


class Command(BaseCommand):
    help = "Analyse compiled Lapse videos for stretches where nothing changes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--session", type=int, action="append", default=[],
            help="Check only this Timelapse id. Repeatable.",
        )
        parser.add_argument(
            "--project", type=int,
            help="Check only the sessions on this project.",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Re-check sessions that have already been analysed.",
        )
        parser.add_argument(
            "--limit", type=int,
            help="Stop after this many sessions.",
        )

    def handle(self, *args, **options):
        if not activity.ffmpeg_available():
            raise CommandError(
                "ffmpeg is not on PATH. The activity checker is one ffmpeg "
                "pass per video and cannot run without it."
            )

        sessions = Timelapse.objects.all()
        if options["session"]:
            sessions = sessions.filter(id__in=options["session"])
        if options["project"]:
            sessions = sessions.filter(project_id=options["project"])
        if not options["force"]:
            sessions = sessions.filter(activity_checked_at__isnull=True)

        sessions = sessions.order_by("created_at", "id")
        if options["limit"]:
            sessions = sessions[: options["limit"]]

        sessions = list(sessions)
        if not sessions:
            self.stdout.write("Nothing to check.")
            return

        checked = failed = 0
        for session in sessions:
            result = activity.check_and_store(session)
            if result is None:
                failed += 1
                self.stderr.write(
                    f"timelapse {session.id} ({session.lapse_id}): "
                    "video unreadable, left unchecked"
                )
                continue
            checked += 1
            segments = len(result["segments"])
            self.stdout.write(
                f"timelapse {session.id} ({session.lapse_id}): "
                f"{result['inactive_percentage']}% inactive across "
                f"{segments} segment{'' if segments == 1 else 's'}"
            )

        self.stdout.write(
            self.style.SUCCESS(f"Checked {checked} session{'' if checked == 1 else 's'}.")
            if checked else "Checked nothing."
        )
        if failed:
            self.stdout.write(
                self.style.WARNING(
                    f"{failed} session{'' if failed == 1 else 's'} could not be read; "
                    "they stay unchecked and will be retried."
                )
            )
