import os
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.urls import reverse

from ..models import (
	AuditLog,
	Item,
	Journal,
	Order,
	Print,
	Project,
	Ship,
	T1,
	T2,
	T3,
)
from .base import (
	VALID_PRINTABLES_URL,
	VALID_R2_URL,
	BaseTestCase,
	User,
	grant_perms,
	make_journal,
	make_project,
	make_ship,
	make_user,
	message_texts,
)

DEFAULT_PFP_ENV = {"DEFAULT_PFP": "https://example.com/default.png"}


class OrganizerOnlyAccessTests(BaseTestCase):
	def _urls(self):
		return [
			reverse("users"),
			reverse("manage_projects"),
			reverse("audit_log"),
			reverse("metrics"),
		]

	@patch.dict(os.environ, DEFAULT_PFP_ENV)
	def test_non_organizers_redirected(self):
		users = [
			make_user("pleb"),
			grant_perms(make_user("t1"), "t1_review"),
			grant_perms(make_user("fulfiller"), "fulfillment"),
		]
		for user in users:
			self.client.force_login(user)
			for url in self._urls():
				with self.subTest(user=user.username, url=url):
					self.assertEqual(self.client.get(url).status_code, 302)

	@patch.dict(os.environ, DEFAULT_PFP_ENV)
	def test_organizer_allowed(self):
		self.client.force_login(grant_perms(make_user("organizer"), "organizer"))
		for url in self._urls():
			with self.subTest(url=url):
				self.assertEqual(self.client.get(url).status_code, 200)


class AdminDashAccessTests(BaseTestCase):
	def test_any_staff_perm_grants_access(self):
		for codename in ("organizer", "fulfillment", "t1_review", "t2_review", "t3_review", "printer"):
			user = grant_perms(make_user(f"dash_{codename}"), codename)
			self.client.force_login(user)
			with self.subTest(perm=codename):
				self.assertEqual(self.client.get(reverse("admin_dash")).status_code, 200)

	def test_regular_user_redirected(self):
		self.client.force_login(make_user("pleb"))
		self.assertEqual(self.client.get(reverse("admin_dash")).status_code, 302)


@patch.dict(os.environ, DEFAULT_PFP_ENV)
class UsersViewTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(make_user("organizer", slack_username="The Organizer"), "organizer")
		self.client.force_login(self.organizer)

	def test_lists_all_users(self):
		make_user("alice", slack_username="alice-slack")
		response = self.client.get(reverse("users"))
		usernames = [u.username for u in response.context["users"]]
		self.assertIn("alice", usernames)
		self.assertIn("organizer", usernames)

	def test_search_by_slack_username(self):
		make_user("alice", slack_username="alice-slack")
		make_user("bob", slack_username="bob-slack")
		response = self.client.get(reverse("users"), {"q": "alice"})
		usernames = [u.username for u in response.context["users"]]
		self.assertEqual(usernames, ["alice"])


class EditUserTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(self.organizer)
		self.target = make_user("target", layers=5)
		self.group = Group.objects.create(name="Reviewers")

	def _edit(self, **overrides):
		data = {
			"editSub": "target",
			"editEmail": "target@example.com",
			"editFirstName": "Tar",
			"editLastName": "Get",
			"editUsername": "target-slack",
			"editSlackId": "U0TARGET",
			"editSlackPfpUrl": "",
			"editLayers": "5",
		}
		data.update(overrides)
		return self.client.post(reverse("edit_user", args=[self.target.id]), data)

	def test_updates_user_and_profile(self):
		self._edit(editEmail="new@example.com", editUsername="new-slack", editLayers="77")
		self.target.refresh_from_db()
		profile = self.target.hackclub_profile
		profile.refresh_from_db()

		self.assertEqual(self.target.email, "new@example.com")
		self.assertEqual(profile.slack_username, "new-slack")
		self.assertEqual(profile.layers, 77)
		self.assertTrue(AuditLog.objects.filter(action="edit_user").exists())

	def test_invalid_layers_ignored(self):
		self._edit(editLayers="not-a-number")
		self.target.hackclub_profile.refresh_from_db()
		self.assertEqual(self.target.hackclub_profile.layers, 5)

	def test_groups_grant_staff_status(self):
		self._edit(groups=[str(self.group.id)])
		self.target.refresh_from_db()
		self.assertTrue(self.target.is_staff)
		self.assertEqual(list(self.target.groups.all()), [self.group])

	def test_removing_groups_revokes_staff_status(self):
		self.target.groups.add(self.group)
		self.target.is_staff = True
		self.target.save()

		self._edit()
		self.target.refresh_from_db()
		self.assertFalse(self.target.is_staff)
		self.assertEqual(self.target.groups.count(), 0)

	def test_valid_pfp_url_updated(self):
		self._edit(editSlackPfpUrl="https://example.com/new-pfp.png")
		self.target.hackclub_profile.refresh_from_db()
		self.assertEqual(
			self.target.hackclub_profile.slack_pfp_url, "https://example.com/new-pfp.png"
		)

	def test_invalid_pfp_url_keeps_previous(self):
		self.image_url_mocks["management"].return_value = False
		self._edit(editSlackPfpUrl="https://example.com/broken")
		self.target.hackclub_profile.refresh_from_db()
		self.assertEqual(self.target.hackclub_profile.slack_pfp_url, "https://example.com/pfp.png")

	def test_unknown_user_404(self):
		response = self.client.post(reverse("edit_user", args=[99999]), {})
		self.assertEqual(response.status_code, 404)


@patch.dict(os.environ, DEFAULT_PFP_ENV)
class ManageProjectsTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(self.organizer)
		self.owner = make_user("owner", slack_username="owner-slack")

	def test_lists_all_projects_including_deleted(self):
		make_project(self.owner, title="Alive")
		make_project(self.owner, title="Dead", deleted=True)
		response = self.client.get(reverse("manage_projects"))
		self.assertEqual(len(response.context["projects"]), 2)

	def test_search_by_title(self):
		make_project(self.owner, title="Benchy Boat")
		make_project(self.owner, title="Calibration Cube")
		response = self.client.get(reverse("manage_projects"), {"q": "benchy"})
		self.assertEqual([p.title for p in response.context["projects"]], ["Benchy Boat"])

	def test_search_by_owner_slack_username(self):
		make_project(self.owner, title="Owned")
		other = make_user("other", slack_username="unrelated")
		make_project(other, title="Other project")
		response = self.client.get(reverse("manage_projects"), {"q": "owner-slack"})
		self.assertEqual([p.title for p in response.context["projects"]], ["Owned"])

	def test_status_annotation(self):
		project = make_project(self.owner)
		make_ship(project, status=Ship.ShipStatus.T2_QUEUE, journal_minutes=(60,))
		response = self.client.get(reverse("manage_projects"))
		annotated = next(p for p in response.context["projects"] if p.id == project.id)
		self.assertEqual(annotated.status_display, "Under T2 Review")
		self.assertEqual(annotated.time_spent_display, "1h 0m")
		self.assertEqual(annotated.journal_count, 1)

	def test_no_ships_status(self):
		make_project(self.owner)
		response = self.client.get(reverse("manage_projects"))
		self.assertEqual(response.context["projects"][0].status_display, "No ships yet")


class AdminEditProjectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(self.organizer)
		self.project = make_project(make_user("owner"), title="Original")

	def _edit(self, **overrides):
		data = {
			"editTitle": "Renamed",
			"editDescription": "New description",
			"editPrintablesUrl": VALID_PRINTABLES_URL,
			"editEditorModelUrl": VALID_R2_URL,
			"editDeleted": "0",
		}
		data.update(overrides)
		return self.client.post(reverse("admin_edit_project", args=[self.project.id]), data)

	def test_edits_project(self):
		self._edit()
		self.project.refresh_from_db()
		self.assertEqual(self.project.title, "Renamed")
		self.assertEqual(self.project.printablesUrl, VALID_PRINTABLES_URL)
		self.assertEqual(self.project.editor_model_url, VALID_R2_URL)
		self.assertFalse(self.project.deleted)

		log = AuditLog.objects.get(action="edit_project")
		self.assertEqual(log.metadata["previous"]["title"], "Original")

	def test_can_soft_delete_and_restore(self):
		self._edit(editDeleted="1")
		self.project.refresh_from_db()
		self.assertTrue(self.project.deleted)

	def test_validations(self):
		cases = [
			{"editTitle": "x" * 61},
			{"editDescription": "x" * 1001},
			{"editPrintablesUrl": "https://thingiverse.com/thing"},
			{"editEditorModelUrl": "https://evil.example.com/model"},
		]
		for overrides in cases:
			with self.subTest(**overrides):
				self._edit(**overrides)
				self.project.refresh_from_db()
				self.assertEqual(self.project.title, "Original")

	def test_blank_urls_allowed(self):
		self._edit(editPrintablesUrl="", editEditorModelUrl="")
		self.project.refresh_from_db()
		self.assertEqual(self.project.title, "Renamed")

	def test_non_organizer_cannot_edit(self):
		self.client.force_login(grant_perms(make_user("t2rev"), "t2_review"))
		self._edit()
		self.project.refresh_from_db()
		self.assertEqual(self.project.title, "Original")


class DbDeleteProjectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(self.organizer)
		self.project = make_project(make_user("owner"), shippable=True)

	def test_hard_deletes_project_with_journals_and_ships(self):
		ship = make_ship(self.project, journal_minutes=(60, 60))
		make_journal(self.project)
		response = self.client.post(reverse("db_delete_project", args=[self.project.id]))

		self.assertIn(f"Removed {self.project.title} from the DB", message_texts(response))
		self.assertFalse(Project.objects.filter(id=self.project.id).exists())
		self.assertFalse(Ship.objects.filter(id=ship.id).exists())
		self.assertEqual(Journal.objects.count(), 0)

	def test_get_not_allowed(self):
		response = self.client.get(reverse("db_delete_project", args=[self.project.id]))
		self.assertEqual(response.status_code, 405)
		self.assertTrue(Project.objects.filter(id=self.project.id).exists())

	def test_unknown_project_404(self):
		response = self.client.post(reverse("db_delete_project", args=[99999]))
		self.assertEqual(response.status_code, 404)


class AuditLogViewTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(self.organizer)
		self.actor = make_user("acting_user", first_name="Acty")
		AuditLog.objects.create(actor=self.actor, action="t1_decision", target="Ship #1 (x)")
		AuditLog.objects.create(actor=self.organizer, action="edit_user", target="User #2 (y)")

	def test_lists_all_logs(self):
		response = self.client.get(reverse("audit_log"))
		self.assertEqual(len(response.context["logs"]), 2)

	def test_filter_by_action(self):
		response = self.client.get(reverse("audit_log"), {"action": "t1_decision"})
		self.assertEqual([log.action for log in response.context["logs"]], ["t1_decision"])

	def test_filter_by_actor(self):
		response = self.client.get(reverse("audit_log"), {"actor": "acting"})
		self.assertEqual([log.actor for log in response.context["logs"]], [self.actor])

	def test_filter_by_actor_first_name(self):
		response = self.client.get(reverse("audit_log"), {"actor": "Acty"})
		self.assertEqual(len(response.context["logs"]), 1)

	def test_filter_by_target_type(self):
		response = self.client.get(reverse("audit_log"), {"target_type": "Ship"})
		self.assertEqual([log.target for log in response.context["logs"]], ["Ship #1 (x)"])

	def test_target_types_derived_from_logs(self):
		response = self.client.get(reverse("audit_log"))
		self.assertEqual(response.context["target_types"], ["Ship", "User"])

	def test_pagination(self):
		for i in range(60):
			AuditLog.objects.create(action=f"bulk_{i}")
		response = self.client.get(reverse("audit_log"))
		self.assertEqual(len(response.context["logs"]), 50)
		response = self.client.get(reverse("audit_log"), {"page": "2"})
		self.assertEqual(len(response.context["logs"]), 12)


class MetricsViewTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(self.organizer)

	def test_renders_with_empty_database(self):
		response = self.client.get(reverse("metrics"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["projects"]["total"], 0)

	def test_renders_with_seeded_data(self):
		author = make_user("author", layers=30)
		reviewer = make_user("reviewer")
		project = make_project(author, shippable=True)
		ship = make_ship(project, status=Ship.ShipStatus.FINALIZED, journal_minutes=(120, 60))
		T1.objects.create(ship=ship, reviewer=reviewer, feedback="", internal_notes="", approved=True)
		T2.objects.create(ship=ship, reviewer=reviewer, decision=T2.Decision.APPROVE,
						  deductions=10, feedback="", justification="")
		T3.objects.create(ship=ship, reviewer=reviewer, decision=T3.Decision.APPROVE,
						  payout_time=120, airtable_time=150, internal_notes="")
		Print.objects.create(ship=ship, printer=reviewer, weight=25,
							 decision=Print.Decision.APPROVE)
		item = Item.objects.create(name="Thing", description="x", cost=5)
		Order.objects.create(owner=author, item=item, status=Order.OrderStatus.FULFILLED,
							 fulfiller=reviewer)
		AuditLog.objects.create(actor=reviewer, action="t1_decision")

		response = self.client.get(reverse("metrics"))
		self.assertEqual(response.status_code, 200)

		context = response.context
		self.assertEqual(context["projects"]["total"], 1)
		self.assertEqual(context["projects"]["total_journals"], 2)
		self.assertEqual(context["ships"]["total"], 1)
		self.assertEqual(context["ships"]["finalized"], 1)
		self.assertEqual(context["reviews"]["t1_total"], 1)
		self.assertEqual(context["reviews"]["t1_approval_rate"], 100.0)
		self.assertEqual(context["reviews"]["total_layers_paid"], 10)
		self.assertEqual(context["reviews"]["print_total_weight"], 25)
		self.assertEqual(context["shop"]["total_orders"], 1)
		self.assertEqual(context["shop"]["layers_spent"], 5)
		self.assertEqual(context["users"]["layers_in_circulation"], 30)
		self.assertGreaterEqual(context["audit"]["total"], 1)
