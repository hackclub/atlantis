from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse

from ..models import Journal, Project, Ship
from .base import (
	VALID_EDITOR_LINK,
	VALID_PRINTABLES_URL,
	BaseTestCase,
	grant_perms,
	image_upload,
	make_journal,
	make_project,
	make_ship,
	make_user,
	message_texts,
	stl_upload,
)


class ProjectListTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("owner")
		self.client.force_login(self.user)

	def test_login_required(self):
		self.client.logout()
		self.assertEqual(self.client.get(reverse("projects")).status_code, 302)

	def test_lists_only_own_non_deleted_projects(self):
		mine = make_project(self.user, title="Mine")
		make_project(self.user, title="Deleted", deleted=True)
		make_project(make_user("other"), title="Theirs")

		response = self.client.get(reverse("projects"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(list(response.context["projects"]), [mine])


class CreateProjectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("creator")
		self.client.force_login(self.user)

	def _create(self, **overrides):
		data = {"title": "New Project", "description": "Something cool.", "printables_url": ""}
		data.update(overrides)
		return self.client.post(reverse("create_project"), data)

	def test_get_not_allowed(self):
		self.assertEqual(self.client.get(reverse("create_project")).status_code, 405)

	def test_creates_project_for_current_user(self):
		response = self._create(printables_url=VALID_PRINTABLES_URL)
		self.assertRedirects(response, reverse("projects"))

		project = Project.objects.get()
		self.assertEqual(project.owner, self.user)
		self.assertEqual(project.title, "New Project")
		self.assertEqual(project.printablesUrl, VALID_PRINTABLES_URL)
		self.assertFalse(project.locked)

	def test_strips_whitespace(self):
		self._create(title="  Padded  ", description="  desc padded  " + "x" * 10)
		project = Project.objects.get()
		self.assertEqual(project.title, "Padded")

	def test_title_required(self):
		response = self._create(title="   ")
		self.assertEqual(Project.objects.count(), 0)
		self.assertIn("Title is required.", message_texts(response))

	def test_title_max_length(self):
		self.assertEqual(self._create(title="x" * 61).status_code, 302)
		self.assertEqual(Project.objects.count(), 0)
		self._create(title="x" * 60)
		self.assertEqual(Project.objects.count(), 1)

	def test_description_required(self):
		self._create(description="")
		self.assertEqual(Project.objects.count(), 0)

	def test_description_max_length(self):
		self._create(description="x" * 1001)
		self.assertEqual(Project.objects.count(), 0)
		self._create(description="x" * 1000)
		self.assertEqual(Project.objects.count(), 1)

	def test_invalid_printables_url_rejected(self):
		response = self._create(printables_url="https://thingiverse.com/thing/1")
		self.assertEqual(Project.objects.count(), 0)
		self.assertIn("Printables URL must be a valid printables.com link.", message_texts(response))


class EditProjectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("editor")
		self.project = make_project(self.user)
		self.client.force_login(self.user)

	def _edit(self, project=None, **overrides):
		data = {"title": "Updated", "description": "Updated description.", "printables_url": ""}
		data.update(overrides)
		project = project or self.project
		return self.client.post(reverse("edit_project", args=[project.id]), data)

	def test_edits_own_project(self):
		self._edit(printables_url=VALID_PRINTABLES_URL)
		self.project.refresh_from_db()
		self.assertEqual(self.project.title, "Updated")
		self.assertEqual(self.project.printablesUrl, VALID_PRINTABLES_URL)

	def test_cannot_edit_other_users_project(self):
		other_project = make_project(make_user("other"))
		self.assertEqual(self._edit(project=other_project).status_code, 404)

	def test_cannot_edit_deleted_project(self):
		self.project.deleted = True
		self.project.save()
		self.assertEqual(self._edit().status_code, 404)

	def test_cannot_edit_locked_project(self):
		self.project.locked = True
		self.project.save()
		response = self._edit()
		self.assertIn("You cannot edit a locked project.", message_texts(response))
		self.project.refresh_from_db()
		self.assertNotEqual(self.project.title, "Updated")

	def test_validations_leave_project_unchanged(self):
		for overrides in ({"title": ""}, {"title": "x" * 61}, {"description": ""},
						  {"description": "x" * 1001}, {"printables_url": "http://bad"}):
			with self.subTest(**overrides):
				self._edit(**overrides)
				self.project.refresh_from_db()
				self.assertEqual(self.project.title, "Test Project")


class DeleteProjectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("deleter")
		self.project = make_project(self.user)
		self.client.force_login(self.user)

	def _delete(self, project=None):
		project = project or self.project
		return self.client.post(reverse("delete_project", args=[project.id]))

	def test_soft_deletes_project(self):
		self._delete()
		self.project.refresh_from_db()
		self.assertTrue(self.project.deleted)

	def test_cannot_delete_locked_project(self):
		self.project.locked = True
		self.project.save()
		self._delete()
		self.project.refresh_from_db()
		self.assertFalse(self.project.deleted)

	def test_cannot_delete_with_ship_in_flight(self):
		for status in (Ship.ShipStatus.T1_QUEUE, Ship.ShipStatus.PRINT_QUEUE,
					   Ship.ShipStatus.BEING_PRINTED, Ship.ShipStatus.T2_QUEUE,
					   Ship.ShipStatus.T3_QUEUE):
			with self.subTest(status=status):
				project = make_project(self.user)
				make_ship(project, status=status, journal_minutes=())
				self._delete(project)
				project.refresh_from_db()
				self.assertFalse(project.deleted)

	def test_can_delete_with_finalized_or_rejected_ships(self):
		for status in (Ship.ShipStatus.FINALIZED, Ship.ShipStatus.REJECTED):
			with self.subTest(status=status):
				project = make_project(self.user)
				make_ship(project, status=status, journal_minutes=())
				self._delete(project)
				project.refresh_from_db()
				self.assertTrue(project.deleted)

	def test_cannot_delete_other_users_project(self):
		other_project = make_project(make_user("other"))
		self.assertEqual(self._delete(other_project).status_code, 404)


class ProjectDetailTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("viewer")
		self.client.force_login(self.user)

	def _detail(self, project):
		return self.client.get(reverse("project_detail", args=[project.id]))

	def test_owner_can_view(self):
		project = make_project(self.user)
		self.assertEqual(self._detail(project).status_code, 200)

	def test_non_owner_gets_404(self):
		project = make_project(make_user("other"))
		self.assertEqual(self._detail(project).status_code, 404)

	def test_can_ship_when_all_requirements_met(self):
		project = make_project(self.user, shippable=True)
		make_journal(project, time_spent=200)
		response = self._detail(project)
		self.assertTrue(response.context["can_ship"])
		self.assertEqual(response.context["ship_disabled_reason"], "")

	def test_cannot_ship_reasons(self):
		cases = [
			("locked", dict(shippable=True, locked=True), "locked"),
			("no printables url", dict(shippable=True, printablesUrl=""), "Printables URL"),
			("no editor model", dict(shippable=True, editor_model_url=""), "editor model"),
		]
		for label, kwargs, expected_fragment in cases:
			with self.subTest(label=label):
				project = make_project(self.user, **kwargs)
				make_journal(project, time_spent=200)
				response = self._detail(project)
				self.assertFalse(response.context["can_ship"])
				self.assertIn(expected_fragment, response.context["ship_disabled_reason"])

	def test_cannot_ship_without_journals(self):
		project = make_project(self.user, shippable=True)
		response = self._detail(project)
		self.assertFalse(response.context["can_ship"])
		self.assertIn("journal", response.context["ship_disabled_reason"])

	def test_cannot_ship_with_pending_ship(self):
		project = make_project(self.user, shippable=True)
		make_ship(project, status=Ship.ShipStatus.T1_QUEUE)
		response = self._detail(project)
		self.assertFalse(response.context["can_ship"])
		self.assertIn("finalized or rejected", response.context["ship_disabled_reason"])

	def test_can_reship_after_rejection(self):
		project = make_project(self.user, shippable=True)
		make_ship(project, status=Ship.ShipStatus.REJECTED)
		response = self._detail(project)
		self.assertTrue(response.context["can_ship"])

	def test_time_spent_totals_journals(self):
		project = make_project(self.user)
		make_journal(project, time_spent=90)
		make_journal(project, time_spent=45)
		response = self._detail(project)
		self.assertEqual(response.context["time_spent"], "2h 15m")

	def test_ship_latest_feedback_uses_most_recent_review(self):
		from ..models import T1
		project = make_project(self.user, shippable=True)
		ship = make_ship(project)
		reviewer = make_user("reviewer")
		T1.objects.create(ship=ship, reviewer=reviewer, feedback="old", internal_notes="", approved=True)
		T1.objects.create(ship=ship, reviewer=reviewer, feedback="newer", internal_notes="", approved=True)
		response = self._detail(project)
		self.assertEqual(response.context["ships"][0].latest_feedback, "newer")

	def test_printables_data_from_api(self):
		self.model_info_mocks[0].return_value = {"makesCount": 7}
		project = make_project(self.user, shippable=True)
		response = self._detail(project)
		self.assertEqual(response.context["printablesData"], {"makesCount": 7})

	def test_printables_api_failure_defaults_to_zero_makes(self):
		self.model_info_mocks[0].side_effect = Exception("api down")
		project = make_project(self.user, shippable=True)
		response = self._detail(project)
		self.assertEqual(response.context["printablesData"], {"makesCount": 0})


class ExploreTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("explorer")
		self.client.force_login(self.user)

	def test_excludes_own_locked_and_deleted_projects(self):
		other = make_user("other")
		visible = make_project(other, title="Visible")
		make_project(other, title="Locked", locked=True)
		make_project(other, title="Deleted", deleted=True)
		make_project(self.user, title="Mine")

		response = self.client.get(reverse("explore"))
		self.assertEqual(list(response.context["projects"]), [visible])


class ProjectDetailExploreTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("visitor")
		self.owner = make_user("owner")
		self.client.force_login(self.user)

	def _detail(self, project):
		return self.client.get(reverse("project_detail_explore", args=[project.id]))

	def test_anyone_logged_in_can_view_unlocked_project(self):
		project = make_project(self.owner)
		self.assertEqual(self._detail(project).status_code, 200)

	def test_locked_project_forbidden_for_regular_users(self):
		project = make_project(self.owner, locked=True)
		self.assertEqual(self._detail(project).status_code, 403)

	def test_locked_project_visible_to_organizer(self):
		project = make_project(self.owner, locked=True)
		organizer = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(organizer)
		self.assertEqual(self._detail(project).status_code, 200)

	def test_deleted_project_404(self):
		project = make_project(self.owner, deleted=True)
		self.assertEqual(self._detail(project).status_code, 404)


@override_settings(ALLOW_JOURNALING=True)
class CreateJournalTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("journaler")
		self.project = make_project(self.user)
		self.client.force_login(self.user)

	def _create(self, project=None, **overrides):
		data = {
			"time_spent": "60",
			"title": "Progress update",
			"text": "x" * 300,
			"image": image_upload(),
			"STL": stl_upload(),
		}
		data.update(overrides)
		data = {k: v for k, v in data.items() if v is not None}
		project = project or self.project
		return self.client.post(reverse("create_journal", args=[project.id]), data)

	def test_get_redirects_without_creating(self):
		response = self.client.get(reverse("create_journal", args=[self.project.id]))
		self.assertEqual(response.status_code, 302)
		self.assertEqual(Journal.objects.count(), 0)

	def test_creates_journal_with_uploaded_files(self):
		response = self._create()
		self.assertIn("Journal entry created successfully", message_texts(response))

		journal = Journal.objects.get()
		self.assertEqual(journal.project, self.project)
		self.assertEqual(journal.time_spent, 60)
		self.assertEqual(journal.title, "Progress update")
		self.assertIsNone(journal.ship)
		# Stored as private-bucket object keys, not public URLs.
		self.assertTrue(journal.image_url.startswith("images/"))
		self.assertTrue(journal.model_url.startswith("models/"))
		self.assertTrue(journal.model_url.endswith(".stl"))

	@override_settings(ALLOW_JOURNALING=False)
	def test_journaling_disabled_blocks_regular_users(self):
		response = self._create()
		self.assertEqual(Journal.objects.count(), 0)
		self.assertIn("Journaling is disallowed on this instance!", message_texts(response))

	@override_settings(ALLOW_JOURNALING=False)
	def test_journaling_disabled_allows_organizer(self):
		organizer = grant_perms(make_user("organizer"), "organizer")
		project = make_project(organizer)
		self.client.force_login(organizer)
		self._create(project=project)
		self.assertEqual(Journal.objects.count(), 1)

	def test_cannot_journal_other_users_project(self):
		other_project = make_project(make_user("other"))
		self.assertEqual(self._create(project=other_project).status_code, 404)

	def test_cannot_journal_locked_project(self):
		self.project.locked = True
		self.project.save()
		self._create()
		self.assertEqual(Journal.objects.count(), 0)

	def test_time_spent_boundaries(self):
		cases = {"29": 0, "30": 1, "240": 1, "241": 0, "abc": 0, "": 0}
		for raw, created in cases.items():
			with self.subTest(time_spent=raw):
				Journal.objects.all().delete()
				self._create(time_spent=raw)
				self.assertEqual(Journal.objects.count(), created)

	def test_text_length_boundaries(self):
		cases = {199: 0, 200: 1, 2000: 1, 2001: 0}
		for length, created in cases.items():
			with self.subTest(length=length):
				Journal.objects.all().delete()
				self._create(text="x" * length)
				self.assertEqual(Journal.objects.count(), created)

	def test_image_required(self):
		response = self._create(image=None)
		self.assertEqual(Journal.objects.count(), 0)
		self.assertIn("An image is required.", message_texts(response))

	def test_stl_required(self):
		response = self._create(STL=None)
		self.assertEqual(Journal.objects.count(), 0)
		self.assertIn("An STL model is required.", message_texts(response))

	def test_model_must_have_stl_extension(self):
		response = self._create(STL=stl_upload(name="model.obj"))
		self.assertEqual(Journal.objects.count(), 0)
		self.assertIn("Uploaded model must be an STL file.", message_texts(response))

	def test_image_size_limit(self):
		from django.core.files.uploadedfile import SimpleUploadedFile
		big = SimpleUploadedFile("big.png", b"\0" * (5 * 1024 * 1024 + 1))
		response = self._create(image=big)
		self.assertEqual(Journal.objects.count(), 0)
		self.assertIn("Max file size for images is 5MB.", message_texts(response))

	def test_image_must_be_real_image(self):
		from django.core.files.uploadedfile import SimpleUploadedFile
		fake = SimpleUploadedFile("fake.png", b"just some text")
		response = self._create(image=fake)
		self.assertEqual(Journal.objects.count(), 0)
		self.assertIn(
			"Uploaded image must be a valid PNG, JPEG, GIF, or WEBP file.",
			message_texts(response),
		)


class ShipProjectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("shipper")
		self.project = make_project(self.user, shippable=True)
		self.client.force_login(self.user)

	def _ship(self, project=None):
		project = project or self.project
		return self.client.post(reverse("ship_project", args=[project.id]))

	def test_get_redirects_without_shipping(self):
		make_journal(self.project, time_spent=200)
		self.client.get(reverse("ship_project", args=[self.project.id]))
		self.assertEqual(Ship.objects.count(), 0)

	def test_first_ship_requires_more_than_180_minutes(self):
		make_journal(self.project, time_spent=180)
		response = self._ship()
		self.assertEqual(Ship.objects.count(), 0)
		self.assertIn(
			"You must have atleast 3 hours of logged time before you can ship!",
			message_texts(response),
		)

	def test_first_ship_with_181_minutes_succeeds(self):
		journal = make_journal(self.project, time_spent=181)
		response = self._ship()
		self.assertIn(
			f'Successfully shipped project "{self.project.title}"!', message_texts(response)
		)
		ship = Ship.objects.get()
		self.assertEqual(ship.status, Ship.ShipStatus.T1_QUEUE)
		journal.refresh_from_db()
		self.assertEqual(journal.ship, ship)

	def test_journal_time_sums_across_entries(self):
		make_journal(self.project, time_spent=100)
		make_journal(self.project, time_spent=100)
		self._ship()
		self.assertEqual(Ship.objects.count(), 1)

	def test_cannot_ship_other_users_project(self):
		other_project = make_project(make_user("other"), shippable=True)
		make_journal(other_project, time_spent=200)
		self.assertEqual(self._ship(other_project).status_code, 404)

	def test_cannot_ship_locked_project(self):
		self.project.locked = True
		self.project.save()
		make_journal(self.project, time_spent=200)
		self._ship()
		self.assertEqual(Ship.objects.count(), 0)

	def test_cannot_ship_without_printables_url(self):
		self.project.printablesUrl = ""
		self.project.save()
		make_journal(self.project, time_spent=200)
		self._ship()
		self.assertEqual(Ship.objects.count(), 0)

	def test_cannot_ship_without_editor_model(self):
		self.project.editor_model_url = ""
		self.project.save()
		make_journal(self.project, time_spent=200)
		response = self._ship()
		self.assertEqual(Ship.objects.count(), 0)
		self.assertIn(
			"You need to upload or link your editor model before you can ship!",
			message_texts(response),
		)

	def test_cannot_ship_without_unassigned_journals(self):
		response = self._ship()
		self.assertEqual(Ship.objects.count(), 0)
		self.assertIn(
			"Your project must have at least one journal to be shipped", message_texts(response)
		)

	def test_cannot_reship_while_ship_in_flight(self):
		make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE)
		make_journal(self.project, time_spent=200)
		response = self._ship()
		self.assertEqual(Ship.objects.count(), 1)
		self.assertIn(
			"You cannot reship until your most recent ship has been finalized or rejected.",
			message_texts(response),
		)

	def test_reship_requires_more_than_120_new_minutes(self):
		make_ship(self.project, status=Ship.ShipStatus.FINALIZED)
		make_journal(self.project, time_spent=120)
		response = self._ship()
		self.assertEqual(Ship.objects.count(), 1)
		self.assertIn(
			"Can't ship again without at least 2 hours of work!", message_texts(response)
		)

	def test_reship_with_121_new_minutes_succeeds(self):
		make_ship(self.project, status=Ship.ShipStatus.FINALIZED)
		new_journal = make_journal(self.project, time_spent=121)
		self._ship()
		self.assertEqual(Ship.objects.count(), 2)
		new_ship = Ship.objects.order_by("-id").first()
		new_journal.refresh_from_db()
		self.assertEqual(new_journal.ship, new_ship)

	def test_reship_only_claims_unassigned_journals(self):
		old_ship = make_ship(self.project, status=Ship.ShipStatus.REJECTED, journal_minutes=(200,))
		old_journal = old_ship.journals.get()
		make_journal(self.project, time_spent=150)
		self._ship()
		old_journal.refresh_from_db()
		self.assertEqual(old_journal.ship, old_ship)


class UpdateEditorModelTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("modeler")
		self.project = make_project(self.user)
		self.client.force_login(self.user)

	def _update(self, project=None, **data):
		project = project or self.project
		return self.client.post(reverse("update_editor_model", args=[project.id]), data)

	def test_link_from_supported_editor_saved(self):
		response = self._update(editor_model_link=VALID_EDITOR_LINK)
		self.assertIn("Editor model updated successfully.", message_texts(response))
		self.project.refresh_from_db()
		self.assertEqual(self.project.editor_model_url, VALID_EDITOR_LINK)

	def test_link_must_be_http(self):
		self._update(editor_model_link="ftp://cad.onshape.com/doc")
		self.project.refresh_from_db()
		self.assertEqual(self.project.editor_model_url, "")

	def test_link_from_unsupported_editor_rejected(self):
		response = self._update(editor_model_link="https://example.com/model")
		self.project.refresh_from_db()
		self.assertEqual(self.project.editor_model_url, "")
		self.assertTrue(any("Unsupported editor model link" in m for m in message_texts(response)))

	def test_requires_file_or_link(self):
		response = self._update()
		self.assertIn(
			"Upload a file or provide a link for the editor model.", message_texts(response)
		)

	def test_locked_project_rejected(self):
		self.project.locked = True
		self.project.save()
		self._update(editor_model_link=VALID_EDITOR_LINK)
		self.project.refresh_from_db()
		self.assertEqual(self.project.editor_model_url, "")

	def test_not_owner_404(self):
		other_project = make_project(make_user("other"))
		self.assertEqual(
			self._update(project=other_project, editor_model_link=VALID_EDITOR_LINK).status_code,
			404,
		)

	@override_settings(ALLOW_JOURNALING=False)
	def test_file_uploads_disabled_when_journaling_off(self):
		from django.core.files.uploadedfile import SimpleUploadedFile
		response = self._update(editor_model_file=SimpleUploadedFile("part.f3d", b"data"))
		self.assertIn("File uploads are currently disabled.", message_texts(response))
		self.project.refresh_from_db()
		self.assertEqual(self.project.editor_model_url, "")

	@override_settings(ALLOW_JOURNALING=True)
	def test_file_upload_saved_to_storage(self):
		from django.core.files.uploadedfile import SimpleUploadedFile
		response = self._update(editor_model_file=SimpleUploadedFile("part.f3d", b"fusion data"))
		self.assertIn("Editor model updated successfully.", message_texts(response))
		self.project.refresh_from_db()
		# Stored as a private-bucket object key, not a public URL.
		self.assertTrue(self.project.editor_model_url.startswith("editor_models/"))
		self.assertTrue(self.project.editor_model_url.endswith(".f3d"))

	@override_settings(ALLOW_JOURNALING=True)
	def test_unsupported_file_extension_rejected(self):
		from django.core.files.uploadedfile import SimpleUploadedFile
		response = self._update(editor_model_file=SimpleUploadedFile("part.stl", b"data"))
		self.assertTrue(any("Unsupported editor model file" in m for m in message_texts(response)))
		self.project.refresh_from_db()
		self.assertEqual(self.project.editor_model_url, "")

	@override_settings(ALLOW_JOURNALING=True)
	def test_oversized_file_rejected(self):
		from django.core.files.uploadedfile import SimpleUploadedFile
		big = SimpleUploadedFile("part.f3d", b"\0" * (50 * 1024 * 1024 + 1))
		response = self._update(editor_model_file=big)
		self.assertIn("Editor model file too large. Max 50MB.", message_texts(response))


class FollowProjectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("follower")
		self.owner = make_user("owner")
		self.project = make_project(self.owner)
		self.client.force_login(self.user)

	def _follow(self, project=None):
		project = project or self.project
		return self.client.post(reverse("follow_project", args=[project.id]))

	def _unfollow(self, project=None):
		project = project or self.project
		return self.client.post(reverse("unfollow_project", args=[project.id]))

	def test_follow_adds_user(self):
		response = self._follow()
		self.assertTrue(self.project.followers.filter(pk=self.user.pk).exists())
		self.assertTrue(any("now following" in m for m in message_texts(response)))

	def test_unfollow_removes_user(self):
		self.project.followers.add(self.user)
		response = self._unfollow()
		self.assertFalse(self.project.followers.filter(pk=self.user.pk).exists())
		self.assertTrue(any("unfollowed" in m for m in message_texts(response)))

	def test_cannot_follow_own_project(self):
		self.client.force_login(self.owner)
		self._follow()
		self.assertFalse(self.project.followers.filter(pk=self.owner.pk).exists())

	def test_cannot_follow_locked_project(self):
		locked = make_project(self.owner, locked=True)
		self.assertEqual(self._follow(locked).status_code, 403)

	def test_get_request_not_allowed(self):
		self.assertEqual(self.client.get(reverse("follow_project", args=[self.project.id])).status_code, 405)

	def test_detail_reports_follow_state(self):
		response = self.client.get(reverse("project_detail_explore", args=[self.project.id]))
		self.assertFalse(response.context["is_following"])
		self.project.followers.add(self.user)
		response = self.client.get(reverse("project_detail_explore", args=[self.project.id]))
		self.assertTrue(response.context["is_following"])

	def test_detail_reports_follower_count(self):
		response = self.client.get(reverse("project_detail_explore", args=[self.project.id]))
		self.assertEqual(response.context["follower_count"], 0)
		self.project.followers.add(self.user, make_user("other_follower"))
		response = self.client.get(reverse("project_detail_explore", args=[self.project.id]))
		self.assertEqual(response.context["follower_count"], 2)


@override_settings(ALLOW_JOURNALING=True)
class FollowerNotificationTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.owner = make_user("owner")
		self.follower = make_user("follower")
		self.project = make_project(self.owner, shippable=True)
		self.project.followers.add(self.follower)
		self.client.force_login(self.owner)

	@patch("atlantis_site.views.client.projects.notify_followers")
	def test_journal_creation_notifies_followers(self, mock_notify):
		self.client.post(
			reverse("create_journal", args=[self.project.id]),
			{
				"time_spent": "60",
				"title": "Update",
				"text": "x" * 300,
				"image": image_upload(),
				"STL": stl_upload(),
			},
		)
		self.assertEqual(Journal.objects.count(), 1)
		mock_notify.assert_called_once()
		args = mock_notify.call_args.args
		self.assertEqual(args[1], self.project)
		self.assertIn("new journal entry", args[2])

	@patch("atlantis_site.views.client.projects.notify_followers")
	def test_ship_notifies_followers(self, mock_notify):
		make_journal(self.project, time_spent=200)
		self.client.post(reverse("ship_project", args=[self.project.id]))
		self.assertEqual(Ship.objects.count(), 1)
		mock_notify.assert_called_once()
		args = mock_notify.call_args.args
		self.assertEqual(args[1], self.project)
		self.assertIn("shipped", args[2])

	@patch("atlantis_site.views.helpers.send_slack_dm")
	def test_notify_followers_dms_followers_with_project_link(self, mock_dm):
		from django.test import RequestFactory

		from atlantis_site.views.helpers import notify_followers

		self.project.followers.add(self.owner)
		request = RequestFactory().get("/")
		notify_followers(request, self.project, "hello")

		expected_url = request.build_absolute_uri(
			reverse("project_detail_explore", args=[self.project.id])
		)
		mock_dm.assert_called_once_with(f"hello {expected_url}", self.follower.hackclub_profile.slack_id)


class ServeMediaTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("viewer")
		self.client.force_login(self.user)

	def _store(self, key, content=b"filedata"):
		from django.core.files.base import ContentFile
		from django.core.files.storage import default_storage
		return default_storage.save(key, ContentFile(content))

	def test_streams_stored_object(self):
		key = self._store("images/abc.png", b"pngbytes")
		response = self.client.get(reverse("serve_media", args=[key]))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(b"".join(response.streaming_content), b"pngbytes")
		self.assertEqual(response["Content-Type"], "image/png")

	def test_requires_login(self):
		self.client.logout()
		response = self.client.get(reverse("serve_media", args=["images/abc.png"]))
		self.assertEqual(response.status_code, 302)

	def test_missing_key_returns_404(self):
		response = self.client.get(reverse("serve_media", args=["images/nope.png"]))
		self.assertEqual(response.status_code, 404)

	def test_rejects_disallowed_prefix(self):
		self._store("secrets/private.txt", b"secret")
		response = self.client.get(reverse("serve_media", args=["secrets/private.txt"]))
		self.assertEqual(response.status_code, 404)

	def test_rejects_path_traversal(self):
		response = self.client.get("/media/images/../secrets/x")
		self.assertEqual(response.status_code, 404)
