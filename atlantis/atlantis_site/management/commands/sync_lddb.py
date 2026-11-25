from django.core.management.base import BaseCommand

from ... import lddb
from ...models import Timelapse


class Command(BaseCommand):
	help = "Push timelapse reviews to LDDB and pull changes made there."

	def add_arguments(self, parser):
		parser.add_argument(
			"--backfill",
			action="store_true",
			help="First queue every signed-off Lapse recording LDDB hasn't had yet.",
		)
		parser.add_argument(
			"--limit",
			type=int,
			default=200,
			help="Most recordings to push in one run (default 200).",
		)
		parser.add_argument(
			"--no-pull",
			action="store_true",
			help="Push only; leave the change feed for the next run.",
		)

	def handle(self, *args, **options):
		if not lddb.enabled():
			self.stdout.write("LDDB_API_KEY isn't set, so there's nothing to sync.")
			return

		if options["backfill"]:
			queued = lddb.queue_backfill()
			self.stdout.write(f"Queued {queued} signed-off recording(s) for LDDB.")

		ids = list(
			Timelapse.objects.filter(lddb_pending=True)
			.order_by("updated_at")
			.values_list("id", flat=True)[: options["limit"]]
		)
		pushed = lddb.push_many(ids)
		left = Timelapse.objects.filter(lddb_pending=True).count()
		self.stdout.write(f"Pushed {pushed} of {len(ids)} recording(s) to LDDB; {left} still waiting.")

		if not options["no_pull"]:
			try:
				applied = lddb.pull()
			except lddb.LddbError as exc:
				self.stderr.write(f"Couldn't read LDDB's changes: {exc}")
				raise SystemExit(1)
			self.stdout.write(f"Applied {applied} change(s) from LDDB.")
