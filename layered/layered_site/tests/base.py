import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from PIL import Image

from ..models import Journal, Profile, Project, Ship

User = get_user_model()

TEST_STORAGES = {
	"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
	"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

VALID_PRINTABLES_URL = "https://www.printables.com/model/12345-cool-thing"
VALID_EDITOR_LINK = "https://cad.onshape.com/documents/abc123"
VALID_R2_URL = "https://pub-d9ac82fd80854a42ae2dde2757ff0a55.r2.dev/models/thing.f3d"

ALL_SITE_PERMS = ["t1_review", "t2_review", "t3_review", "printer", "fulfillment", "organizer"]


def make_user(username="user", layers=0, slack_id="U0TEST", slack_username=None, **user_kwargs):
	"""Create a user with an attached hackclub Profile (as auth_callback would)."""
	user = User.objects.create_user(username=username, password="pw", **user_kwargs)
	Profile.objects.create(
		user=user,
		slack_id=slack_id,
		slack_username=slack_username if slack_username is not None else username,
		slack_pfp_url="https://example.com/pfp.png",
		layers=layers,
	)
	return user


def grant_perms(user, *codenames):
	"""Grant layered_site custom permissions and mark the user as staff."""
	perms = Permission.objects.filter(
		content_type__app_label="layered_site", codename__in=codenames
	)
	assert perms.count() == len(codenames), f"missing perms among {codenames}"
	user.user_permissions.add(*perms)
	user.is_staff = True
	user.save()

	return User.objects.get(pk=user.pk)


def make_project(owner, shippable=False, **kwargs):
	defaults = {"title": "Test Project", "description": "A test project."}
	if shippable:
		defaults["printablesUrl"] = VALID_PRINTABLES_URL
		defaults["editor_model_url"] = VALID_EDITOR_LINK
	defaults.update(kwargs)
	return Project.objects.create(owner=owner, **defaults)


def make_journal(project, ship=None, time_spent=60, **kwargs):
	defaults = {
		"title": "Journal entry",
		"text": "x" * 200,
		"image_url": "https://example.com/image.png",
		"model_url": "https://example.com/model.stl",
	}
	defaults.update(kwargs)
	return Journal.objects.create(project=project, ship=ship, time_spent=time_spent, **defaults)


def make_ship(project, status=Ship.ShipStatus.T1_QUEUE, journal_minutes=(120, 120)):
	"""Create a ship in the given status with journals attached to it."""
	ship = Ship.objects.create(project=project, status=status)
	for minutes in journal_minutes:
		make_journal(project, ship=ship, time_spent=minutes)
	return ship


def image_upload(name="test.png", fmt="PNG", size=(4, 4)):
	buf = io.BytesIO()
	Image.new("RGB", size, color=(200, 30, 30)).save(buf, format=fmt)
	return SimpleUploadedFile(name, buf.getvalue(), content_type=f"image/{fmt.lower()}")


def stl_upload(name="model.stl", content=b"solid test\nendsolid test\n"):
	return SimpleUploadedFile(name, content, content_type="model/stl")


def message_texts(response):
	return [str(m) for m in get_messages(response.wsgi_request)]


@override_settings(STORAGES=TEST_STORAGES, MEDIA_URL="/media/")
class BaseTestCase(TestCase):
	SLACK_DM_TARGETS = [
		"layered_site.views.admin.review.send_slack_dm",
		"layered_site.views.admin.print.send_slack_dm",
		"layered_site.views.admin.shop.send_slack_dm",
	]
	MODEL_INFO_TARGETS = [
		"layered_site.views.client.projects.get_model_info",
		"layered_site.views.admin.review.get_model_info",
	]
	IMAGE_URL_TARGETS = [
		"layered_site.views.admin.print.is_valid_image_url",
		"layered_site.views.admin.shop.is_valid_image_url",
		"layered_site.views.admin.management.is_valid_image_url",
	]

	def setUp(self):
		super().setUp()
		self.slack_dm_mocks = {}
		for target in self.SLACK_DM_TARGETS:
			patcher = patch(target, return_value=True)
			self.slack_dm_mocks[target.rsplit(".", 2)[-2]] = patcher.start()
			self.addCleanup(patcher.stop)

		self.model_info_mocks = []
		for target in self.MODEL_INFO_TARGETS:
			patcher = patch(target, return_value={"makesCount": 0})
			self.model_info_mocks.append(patcher.start())
			self.addCleanup(patcher.stop)

		self.image_url_mocks = {}
		for target in self.IMAGE_URL_TARGETS:
			patcher = patch(target, return_value=True)
			self.image_url_mocks[target.rsplit(".", 2)[-2]] = patcher.start()
			self.addCleanup(patcher.stop)
