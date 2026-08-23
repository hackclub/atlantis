"""Retry the Airtable submissions that didn't go through on finalization.

Finalizing a ship submits it straight away, so this is the safety net for the
runs that failed — Airtable down, credentials not yet granted write access, a
transient 5xx. It only picks up ships whose last attempt is known to have
created nothing, so running it twice cannot duplicate a record.
"""

from django.core.management.base import BaseCommand

from ...airtable import missing_settings
from ...models import AirtableSubmission, Ship
from ...submissions import pending_ships, submit_ship


class Command(BaseCommand):
	help = "Submit finalized ships that have no Airtable record yet."

	def add_arguments(self, parser):
		parser.add_argument(
			"--ship",
			type=int,
			action="append",
			dest="ships",
			help="Only this ship id. Repeatable.",
		)
		parser.add_argument(
			"--dry-run",
			action="store_true",
			help="List what would be submitted without sending anything.",
		)

	def handle(self, *args, **options):
		missing = missing_settings()
		if missing:
			self.stderr.write(
				self.style.ERROR(
					f"Airtable is not configured (missing {', '.join(missing)})."
				)
			)
			return

		ships = pending_ships()
		if options["ships"]:
			ships = ships.filter(id__in=options["ships"])

		ships = list(ships)
		if not ships:
			self.stdout.write("Nothing to submit.")
			self._report_stuck()
			return

		if options["dry_run"]:
			for ship in ships:
				self.stdout.write(f"would submit ship {ship.id} ({ship.project.title})")
			self.stdout.write(f"{len(ships)} ship(s) pending.")
			return

		submitted = 0
		for ship in ships:
			submission = submit_ship(ship)
			label = f"ship {ship.id} ({ship.project.title})"
			if submission.status == AirtableSubmission.Status.SUBMITTED:
				submitted += 1
				self.stdout.write(
					self.style.SUCCESS(f"{label} -> {submission.record_id}")
				)
			else:
				self.stderr.write(
					self.style.ERROR(
						f"{label} -> {submission.get_status_display().lower()}: "
						f"{submission.error}"
					)
				)

		self.stdout.write(f"Submitted {submitted} of {len(ships)}.")
		self._report_stuck()

	def _report_stuck(self):
		"""Name the submissions this command deliberately won't touch.

		A row left in `sending` means a request went out and its outcome was
		never learned. Retrying it is how a project ends up in HQ's table twice,
		so it needs somebody to look the record up by hand.
		"""
		stuck = AirtableSubmission.objects.filter(
			status=AirtableSubmission.Status.SENDING,
			ship__status=Ship.ShipStatus.FINALIZED,
		).select_related("ship__project")
		for submission in stuck:
			self.stderr.write(
				self.style.WARNING(
					f"ship {submission.ship_id} "
					f"({submission.ship.project.title}) is unresolved and was "
					f"skipped — check Airtable by hand: {submission.error}"
				)
			)
