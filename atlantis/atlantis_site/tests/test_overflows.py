"""Values a form can post that the database would refuse.

Every case here used to be a 500. Postgres answers an over-long CharField with
a DataError and an out-of-range integer with NumericValueOutOfRange, and both
come out of the driver as an unhandled exception — so a printables link a few
characters too long, or a quantity with too many digits in it, took the whole
request down instead of coming back as a sentence about the field.

The rule the fixes follow, and what these tests pin, is that a value the
database won't take is either refused with a message (anything the user typed,
where being told is the point) or trimmed to fit (anything an upstream service
said about them, which is not theirs to correct). Nothing reaches a column it
doesn't fit.

The widths are read off the models rather than written down, so a migration
that widens a column doesn't leave a test asserting the old number.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from ..models import Item, Journal, LapseAccount, Order, Profile, Project
from ..views.helpers import INT_FIELD_MAX, field_max_length
from .base import (
	BaseTestCase, VALID_EDITOR_LINK, VALID_PRINTABLES_URL, grant_perms,
	image_upload, make_lookout, make_project, make_user, message_texts,
	stl_upload, shop_items,
)

User = get_user_model()


def over(model, field_name, filler="x"):
	"""A value one character too wide for `model.field_name`."""
	return filler * (field_max_length(model, field_name) + 1)


class ProjectFieldWidthTests(BaseTestCase):
	"""printablesUrl and is_valid_printables_url both stop at 2048 chars, so an
	over-long link is refused as invalid before the width check is reached —
	either way, a sentence rather than a 500."""

	def setUp(self):
		super().setUp()
		self.user = make_user("builder")
		self.client.force_login(self.user)

	def _long_printables_url(self):
		# A real, valid printables.com link that simply runs past the column —
		# the slug is the part that gets long in practice.
		limit = field_max_length(Project, "printablesUrl")
		url = VALID_PRINTABLES_URL + "-" + "a" * limit
		self.assertGreater(len(url), limit)
		return url

	def test_create_rejects_over_long_printables_url(self):
		response = self.client.post(reverse("create_project"), {
			"title": "Thing",
			"description": "A thing.",
			"printables_url": self._long_printables_url(),
		})
		self.assertEqual(response.status_code, 302)
		self.assertEqual(Project.objects.count(), 0)
		self.assertTrue(any("Printables URL" in m for m in message_texts(response)))

	def test_edit_rejects_over_long_printables_url(self):
		project = make_project(self.user, printablesUrl=VALID_PRINTABLES_URL)
		response = self.client.post(reverse("edit_project", args=[project.id]), {
			"title": "Thing",
			"description": "A thing.",
			"printables_url": self._long_printables_url(),
		})
		self.assertEqual(response.status_code, 302)
		project.refresh_from_db()
		self.assertEqual(project.printablesUrl, VALID_PRINTABLES_URL)

	def test_editor_model_link_longer_than_its_column_is_refused(self):
		project = make_project(self.user)
		link = VALID_EDITOR_LINK + "?q=" + "a" * field_max_length(Project, "editor_model_url")
		response = self.client.post(reverse("update_editor_model", args=[project.id]), {
			"editor_model_link": link,
		})
		self.assertEqual(response.status_code, 302)
		project.refresh_from_db()
		self.assertEqual(project.editor_model_url, "")
		self.assertTrue(any("too long" in m for m in message_texts(response)))


class JournalTitleWidthTests(BaseTestCase):
	"""A lapse title was only ever checked for being empty."""

	def setUp(self):
		super().setUp()
		self.user = make_user("builder")
		self.client.force_login(self.user)
		self.project = make_project(self.user)

	def test_over_long_title_is_refused(self):
		# Footage first: the attach is validated before the title is looked at.
		recording = make_lookout(self.project)
		response = self.client.post(reverse("create_journal", args=[self.project.id]), {
			"title": over(Journal, "title"),
			"lookout_timelapses": [str(recording.id)],
			"image": image_upload(),
			"STL": stl_upload(),
		})
		self.assertEqual(response.status_code, 302)
		self.assertEqual(Journal.objects.count(), 0)
		self.assertTrue(any("too long" in m for m in message_texts(response)))


class OrderFieldTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("shopper", layers=1000)
		self.client.force_login(self.user)
		self.item = Item.objects.create(name="Filament", description="PLA", cost=10)

	def _order(self, **overrides):
		data = {"quantity": "1", "user_notes": ""}
		data.update(overrides)
		return self.client.post(reverse("order_item", args=[self.item.id]), data)

	def test_over_long_notes_are_refused(self):
		response = self._order(user_notes=over(Order, "user_notes"))
		self.assertEqual(response.status_code, 302)
		self.assertEqual(Order.objects.count(), 0)
		self.assertTrue(any("too long" in m for m in message_texts(response)))

	def test_notes_at_the_limit_are_accepted(self):
		notes = "x" * field_max_length(Order, "user_notes")
		self._order(user_notes=notes)
		self.assertEqual(Order.objects.get().user_notes, notes)

	def test_quantity_past_the_column_is_refused(self):
		# A free, unlimited-stock item clears both the affordability and the
		# stock gate, so nothing but the column stands between this and the
		# driver — which is exactly where it used to 500.
		free = Item.objects.create(name="Sticker", description="free", cost=0, stock=-1)
		response = self.client.post(reverse("order_item", args=[free.id]), {
			"quantity": str(INT_FIELD_MAX + 1),
			"user_notes": "",
		})
		self.assertEqual(response.status_code, 302)
		self.assertEqual(Order.objects.count(), 0)

	def test_address_id_wider_than_its_column_is_dropped_not_stored(self):
		# Fulfillment falls back to the primary address when there is no id, so
		# an unusable one is the same situation as not getting one at all.
		with patch.object(
			Profile, "primary_address_id",
			property(lambda self: "a" * (field_max_length(Order, "address_id") + 1)),
		):
			self._order()
		self.assertEqual(Order.objects.get().address_id, "")


class AdminItemFieldTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.staff = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(self.staff)

	def _create(self, **overrides):
		data = {
			"name": "Filament",
			"description": "PLA",
			"cost": "10",
			"imageUrl": "https://example.com/i.png",
			"category": "Other",
			"stock": "-1",
		}
		data.update(overrides)
		return self.client.post(reverse("create_item"), data)

	def test_negative_cost_is_refused(self):
		# cost is a PositiveIntegerField, so a negative one is a CHECK
		# violation rather than a validation message.
		response = self._create(cost="-5")
		self.assertEqual(shop_items().count(), 0)
		self.assertTrue(any("negative" in m for m in message_texts(response)))

	def test_cost_past_the_column_is_refused(self):
		self._create(cost=str(INT_FIELD_MAX + 1))
		self.assertEqual(shop_items().count(), 0)

	def test_stock_past_the_column_is_refused(self):
		self._create(stock=str(INT_FIELD_MAX + 1))
		self.assertEqual(shop_items().count(), 0)

	def test_over_long_text_fields_are_refused(self):
		for field, key in (("name", "name"), ("description", "description"),
						   ("category", "category")):
			with self.subTest(field=field):
				self._create(**{key: over(Item, field)})
				self.assertEqual(shop_items().count(), 0)

	def test_edit_refuses_a_negative_cost_and_leaves_the_item_alone(self):
		item = Item.objects.create(name="Filament", description="PLA", cost=10)
		self.client.post(reverse("edit_item", args=[item.id]), {
			"name": "Filament",
			"description": "PLA",
			"cost": "-1",
			"imageUrl": "https://example.com/i.png",
			"category": "Other",
			"stock": "-1",
		})
		item.refresh_from_db()
		self.assertEqual(item.cost, 10)


class EditUserTests(BaseTestCase):
	"""The required-field loop reported its complaints and wrote the row anyway."""

	def setUp(self):
		super().setUp()
		self.staff = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(self.staff)
		self.target = make_user("target", slack_id="U0TARGET")

	def _edit(self, **overrides):
		data = {
			"editSub": "target",
			"editEmail": "target@example.com",
			"editFirstName": "Tar",
			"editLastName": "Get",
			"editUsername": "target",
			"editSlackId": "U0TARGET",
			"editSlackPfpUrl": "",
			"editLayers": "0",
		}
		data.update(overrides)
		return self.client.post(reverse("edit_user", args=[self.target.id]), data)

	def test_missing_required_field_stops_the_write(self):
		response = self._edit(editSub="")
		self.assertEqual(response.status_code, 302)
		self.target.refresh_from_db()
		self.assertEqual(self.target.username, "target")
		self.assertTrue(any("required" in m for m in message_texts(response)))

	def test_over_long_username_is_refused(self):
		self._edit(editSub=over(User, "username"))
		self.target.refresh_from_db()
		self.assertEqual(self.target.username, "target")

	def test_over_long_slack_username_is_refused(self):
		self._edit(editUsername=over(Profile, "slack_username"))
		self.target.hackclub_profile.refresh_from_db()
		self.assertEqual(self.target.hackclub_profile.slack_username, "target")

	def test_username_already_taken_is_refused(self):
		response = self._edit(editSub="organizer")
		self.target.refresh_from_db()
		self.assertEqual(self.target.username, "target")
		self.assertTrue(any("already has" in m for m in message_texts(response)))

	def test_layers_past_the_column_are_refused(self):
		self._edit(editLayers=str(INT_FIELD_MAX + 1))
		self.target.hackclub_profile.refresh_from_db()
		self.assertEqual(self.target.hackclub_profile.layers, 0)

	def test_non_numeric_group_ids_are_ignored_rather_than_raised(self):
		group = Group.objects.create(name="reviewers")
		self._edit(groups=[str(group.id), "not-an-id", "999999"])
		self.assertEqual(list(self.target.groups.all()), [group])

	def test_a_valid_edit_still_goes_through(self):
		self._edit(editFirstName="Renamed", editLayers="42")
		self.target.refresh_from_db()
		self.target.hackclub_profile.refresh_from_db()
		self.assertEqual(self.target.first_name, "Renamed")
		self.assertEqual(self.target.hackclub_profile.layers, 42)


class AdminEditProjectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.staff = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(self.staff)
		self.project = make_project(make_user("owner"), printablesUrl=VALID_PRINTABLES_URL)

	def test_over_long_printables_url_is_refused(self):
		limit = field_max_length(Project, "printablesUrl")
		self.client.post(reverse("admin_edit_project", args=[self.project.id]), {
			"editTitle": "Thing",
			"editDescription": "A thing.",
			"editPrintablesUrl": VALID_PRINTABLES_URL + "-" + "a" * limit,
			"editEditorModelUrl": "",
		})
		self.project.refresh_from_db()
		self.assertEqual(self.project.printablesUrl, VALID_PRINTABLES_URL)

	def test_over_long_editor_model_url_is_refused(self):
		limit = field_max_length(Project, "editor_model_url")
		self.client.post(reverse("admin_edit_project", args=[self.project.id]), {
			"editTitle": "Thing",
			"editDescription": "A thing.",
			"editPrintablesUrl": VALID_PRINTABLES_URL,
			"editEditorModelUrl": VALID_EDITOR_LINK + "?q=" + "a" * limit,
		})
		self.project.refresh_from_db()
		self.assertEqual(self.project.editor_model_url, "")


class MissingProfileTests(BaseTestCase):
	"""An account that never came through the HCA login has no profile row.

	`createsuperuser` makes one of those, and reading `.hackclub_profile` off
	it raises RelatedObjectDoesNotExist — a 500 in the middle of a refund.
	"""

	def setUp(self):
		super().setUp()
		self.staff = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(self.staff)
		self.profileless = User.objects.create_user(username="profileless", password="pw")
		self.item = Item.objects.create(name="Filament", description="PLA", cost=10)

	def test_refunding_an_order_from_a_profileless_user_works(self):
		order = Order.objects.create(
			owner=self.profileless, item=self.item, quantity=2, cost=10,
		)
		response = self.client.post(
			reverse("update_order_status", args=[order.id]), {"action": "refunded"}
		)
		self.assertEqual(response.status_code, 302)
		order.refresh_from_db()
		self.assertEqual(order.status, Order.OrderStatus.REFUNDED)
		self.assertEqual(Profile.objects.get(user=self.profileless).layers, 20)

	def test_unknown_order_id_is_a_404_not_a_500(self):
		response = self.client.post(
			reverse("update_order_status", args=[999999]), {"action": "fulfilled"}
		)
		self.assertEqual(response.status_code, 404)


class AuthCallbackTests(BaseTestCase):
	"""What HCA and Slack say about someone is trimmed, never refused.

	Bouncing a login over a display name the user didn't choose would lock them
	out of the site; failing the write did exactly that, with a 500.
	"""

	def _callback(self, userinfo):
		token = {"userinfo": userinfo}
		with patch("atlantis_site.views.client.auth.oauth") as oauth, \
			 patch("atlantis_site.views.client.auth.slack_client") as slack:
			oauth.hackclub.authorize_access_token.return_value = token
			slack.users_info.side_effect = Exception("slack down")
			return self.client.get(reverse("auth_callback"))

	def test_identity_without_a_slack_id_logs_in(self):
		# display_name and avatar_url were only ever bound inside the branch
		# this identity skips, so reading them back was a NameError.
		response = self._callback({"sub": "hca|nobody", "name": "No Slack"})
		self.assertEqual(response.status_code, 302)
		profile = Profile.objects.get(user__username="hca|nobody")
		self.assertEqual(profile.slack_username, "No Slack")

	def test_over_long_names_are_trimmed_to_their_columns(self):
		self._callback({
			"sub": "hca|verbose",
			"name": "n" * 500,
			"given_name": "g" * 500,
			"family_name": "f" * 500,
			"email": "verbose@example.com",
		})
		user = User.objects.get(username="hca|verbose")
		self.assertEqual(len(user.first_name), field_max_length(User, "first_name"))
		self.assertEqual(
			len(user.hackclub_profile.slack_username),
			field_max_length(Profile, "slack_username"),
		)

	def test_identity_with_no_sub_does_not_raise(self):
		response = self._callback({"name": "Nobody"})
		self.assertEqual(response.status_code, 302)
		self.assertEqual(User.objects.filter(username="").count(), 0)


class LapseAccountWidthTests(BaseTestCase):
	"""Same rule for Lapse: its strings are trimmed, not bounced."""

	def setUp(self):
		super().setUp()
		self.user = make_user("builder")
		self.client.force_login(self.user)

	def test_over_long_profile_strings_are_trimmed(self):
		session = self.client.session
		session["lapse_oauth"] = {"state": "st", "verifier": "vf", "next": ""}
		session.save()

		module = "atlantis_site.views.client.lapse.lapse"
		with patch(f"{module}.exchange_code", return_value={"access_token": "t"}), \
			 patch(f"{module}.token_names_a_user", return_value={
				 "id": "i" * 500,
				 "handle": "h" * 500,
				 "displayName": "d" * 500,
				 "profilePictureUrl": "https://example.com/" + "p" * 500,
			 }):
			response = self.client.get(reverse("lapse_callback"), {"code": "c", "state": "st"})

		self.assertEqual(response.status_code, 302)
		account = LapseAccount.objects.get(user=self.user)
		for field in ("lapse_user_id", "handle", "display_name", "profile_picture_url"):
			with self.subTest(field=field):
				self.assertEqual(
					len(getattr(account, field)), field_max_length(LapseAccount, field)
				)
