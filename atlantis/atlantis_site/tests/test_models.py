from django.test import TestCase

from django.db import transaction

from ..models import AuditLog, Item, LapsesUnreviewed, Order, Ship, T1, Timelapse, media_url
from .base import (
	VALID_EDITOR_LINK, approve_timelapse, make_journal, make_project, make_ship,
	make_timelapse, make_user,
)


class MediaUrlTests(TestCase):
	def test_blank_returns_empty(self):
		self.assertEqual(media_url(""), "")

	def test_external_link_passes_through(self):
		link = "https://cad.onshape.com/documents/abc123"
		self.assertEqual(media_url(link), link)

	def test_object_key_resolves_to_proxy_url(self):
		self.assertEqual(media_url("images/abc.png"), "/media/images/abc.png")

	def test_project_and_journal_display_properties(self):
		project = make_project(make_user(), editor_model_url="editor_models/x.f3d")
		self.assertEqual(project.editor_model_display_url, "/media/editor_models/x.f3d")


class ProfileModelTests(TestCase):
	def test_str_is_username(self):
		user = make_user("someuser")
		self.assertEqual(str(user.hackclub_profile), "someuser")

	def test_layers_default_zero(self):
		user = make_user("fresh")
		self.assertEqual(user.hackclub_profile.layers, 0)


class ProjectModelTests(TestCase):
	def test_str_includes_id_and_title(self):
		project = make_project(make_user(), title="Benchy")
		self.assertEqual(str(project), f"{project.id}: Benchy")

	def test_editor_name_from_link(self):
		project = make_project(make_user(), editor_model_url=VALID_EDITOR_LINK)
		self.assertEqual(project.editor_name, "Onshape")

	def test_editor_name_from_uploaded_file_url(self):
		project = make_project(make_user(), editor_model_url="https://cdn.example.com/editor_models/abc.f3d")
		self.assertEqual(project.editor_name, "Fusion 360")

	def test_editor_name_none_when_unset(self):
		project = make_project(make_user())
		self.assertIsNone(project.editor_name)

	def test_defaults(self):
		project = make_project(make_user())
		self.assertFalse(project.locked)
		self.assertFalse(project.deleted)


class ShipModelTests(TestCase):
	def test_default_status_is_t1_queue(self):
		project = make_project(make_user())
		ship = Ship.objects.create(project=project)
		self.assertEqual(ship.status, Ship.ShipStatus.T1_QUEUE)

	def test_str_mentions_status(self):
		project = make_project(make_user())
		ship = Ship.objects.create(project=project)
		self.assertIn("T1", str(ship))


class UnreviewedLapsesNeverPassT1Tests(TestCase):
	"""The model-level floor under the T1 views: whatever path a ship takes,
	it can't be decided at T1 or leave the T1 queue with a lapse unreviewed."""

	def setUp(self):
		self.reviewer = make_user("t1rev")
		self.project = make_project(make_user("shipper"))
		self.ship = make_ship(self.project, timelapse_approved=False)

	def test_no_t1_decision_of_any_verdict_on_an_uncleared_ship(self):
		for approved, changes in ((True, False), (False, True), (False, False)):
			with self.subTest(approved=approved, changes=changes):
				with self.assertRaises(LapsesUnreviewed):
					T1.objects.create(
						ship=self.ship, reviewer=self.reviewer, feedback="", internal_notes="",
						approved=approved, changes_requested=changes,
					)
		self.assertFalse(T1.objects.exists())

	def test_one_unreviewed_lapse_among_reviewed_ones_still_blocks(self):
		journals = list(self.ship.journals.all())
		for journal in journals[1:]:
			approve_timelapse(journal)
		with self.assertRaises(LapsesUnreviewed):
			T1.objects.create(
				ship=self.ship, reviewer=self.reviewer, feedback="", internal_notes="", approved=True,
			)

	def test_an_uncleared_ship_cannot_leave_the_t1_queue(self):
		for status in Ship.ShipStatus:
			if status == Ship.ShipStatus.T1_QUEUE:
				continue
			with self.subTest(status=status):
				self.ship.status = status
				with self.assertRaises(LapsesUnreviewed):
					with transaction.atomic():
						self.ship.save()
				self.ship.refresh_from_db()
				self.assertEqual(self.ship.status, Ship.ShipStatus.T1_QUEUE)

	def test_a_cleared_ship_passes_t1_as_usual(self):
		for journal in self.ship.journals.all():
			approve_timelapse(journal)
		T1.objects.create(
			ship=self.ship, reviewer=self.reviewer, feedback="", internal_notes="", approved=True,
		)
		self.ship.status = Ship.ShipStatus.T2_QUEUE
		self.ship.save()
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T2_QUEUE)

	def test_footage_cannot_be_added_to_a_signed_off_lapse(self):
		journal = make_journal(self.project, ship=self.ship)
		approve_timelapse(journal)
		with self.assertRaises(ValueError):
			make_timelapse(self.project, journal=journal)

		loose = make_timelapse(self.project)
		loose.journal = journal
		with self.assertRaises(ValueError):
			loose.save()
		loose.refresh_from_db()
		self.assertIsNone(loose.journal_id)

	def test_footage_already_on_a_signed_off_lapse_still_saves(self):
		journal = make_journal(self.project, ship=self.ship)
		session = journal.timelapses.get()
		approve_timelapse(journal)
		session.status = Timelapse.Status.COMPLETE
		session.save()
		session.save(update_fields=["status"])


class ItemModelTests(TestCase):
	def test_str(self):
		item = Item.objects.create(name="Filament", description="1kg PLA", cost=30)
		self.assertEqual(str(item), "Filament (1kg PLA) for 30 layers")

	def test_defaults(self):
		item = Item.objects.create(name="Filament", description="1kg PLA", cost=30)
		self.assertFalse(item.deleted)
		self.assertEqual(item.category, "Other")


class OrderModelTests(TestCase):
	def setUp(self):
		self.user = make_user("buyer")
		self.item = Item.objects.create(name="Filament", description="1kg", cost=25)

	def test_save_defaults_cost_to_item_cost(self):
		order = Order.objects.create(owner=self.user, item=self.item)
		self.assertEqual(order.cost, 25)

	def test_save_keeps_explicit_cost(self):
		order = Order.objects.create(owner=self.user, item=self.item, cost=10)
		self.assertEqual(order.cost, 10)

	def test_save_keeps_explicit_zero_cost(self):
		# A free order must not silently re-inherit the item's price.
		order = Order.objects.create(owner=self.user, item=self.item, cost=0)
		self.assertEqual(order.cost, 0)

	def test_cost_snapshot_survives_item_price_change(self):
		order = Order.objects.create(owner=self.user, item=self.item)
		self.item.cost = 99
		self.item.save()
		order.refresh_from_db()
		self.assertEqual(order.cost, 25)

	def test_defaults(self):
		order = Order.objects.create(owner=self.user, item=self.item)
		self.assertEqual(order.status, Order.OrderStatus.PENDING)
		self.assertEqual(order.quantity, 1)
		self.assertIsNone(order.fulfiller)
		self.assertIsNone(order.refunded)


class AuditLogModelTests(TestCase):
	def test_str_with_actor(self):
		user = make_user("actor")
		log = AuditLog.objects.create(actor=user, action="did_thing")
		self.assertIn("actor", str(log))
		self.assertIn("did_thing", str(log))

	def test_str_with_deleted_actor(self):
		log = AuditLog.objects.create(actor=None, action="did_thing")
		self.assertIn("deleted user", str(log))

	def test_default_ordering_newest_first(self):
		first = AuditLog.objects.create(action="one")
		second = AuditLog.objects.create(action="two")
		self.assertEqual(list(AuditLog.objects.all()), [second, first])
