from django.test import TestCase

from ..models import AuditLog, Item, Order, Ship
from .base import VALID_EDITOR_LINK, make_project, make_user


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
