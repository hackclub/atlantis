import io
import itertools
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from cryptography.fernet import Fernet
from PIL import Image

from ..models import (
	Journal, LookoutSession, Profile, Project, Ship, TimelapseRemoval,
	TimelapseReview,
)

User = get_user_model()

TEST_STORAGES = {
	"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
	"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()

VALID_PRINTABLES_URL = "https://www.printables.com/model/12345-cool-thing"
VALID_EDITOR_LINK = "https://cad.onshape.com/documents/abc123"
VALID_R2_URL = "https://pub-d9ac82fd80854a42ae2dde2757ff0a55.r2.dev/models/thing.f3d"
PROJECT_IMAGE_KEY = "images/screenshot.png"

ALL_SITE_PERMS = [
	"t1_review", "t2_review", "t3_review", "timelapse_review", "fulfillment",
	"organizer",
]


def make_user(
	username="user", layers=0, slack_id="U0TEST", slack_username=None, hca_token=None,
	verification_status="verified", ysws_eligible=True, **user_kwargs
):
	"""Create a user with an attached hackclub Profile (as auth_callback would).

	Pass hca_token to give the profile stored HCA credentials — needed by
	anything that fetches the user's address.

	The default profile is verified and YSWS-eligible, which is what an ordinary
	user is; pass verification_status/ysws_eligible to make one HCA has turned
	down (or not yet ruled on).
	"""
	user = User.objects.create_user(username=username, password="pw", **user_kwargs)
	profile = Profile.objects.create(
		user=user,
		verification_status=verification_status,
		ysws_eligible=ysws_eligible,
		slack_id=slack_id,
		slack_username=slack_username if slack_username is not None else username,
		slack_pfp_url="https://example.com/pfp.png",
		layers=layers,
	)
	if hca_token:
		profile.save_hca_token(hca_token)
	return user


def grant_perms(user, *codenames):
	"""Grant atlantis_site custom permissions and mark the user as staff."""
	perms = Permission.objects.filter(
		content_type__app_label="atlantis_site", codename__in=codenames
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
		defaults["image_url"] = PROJECT_IMAGE_KEY
	defaults.update(kwargs)
	return Project.objects.create(owner=owner, **defaults)


_timelapse_seq = itertools.count(1)


def make_timelapse(project, journal=None, minutes=60, owner=None, **kwargs):
	"""Create a finished Lookout session — the only source of tracked time."""
	n = next(_timelapse_seq)
	defaults = {
		"session_id": f"session-{n}",
		"token": f"token-{n}",
		"status": LookoutSession.Status.COMPLETE,
		"tracked_seconds": minutes * 60,
	}
	defaults.update(kwargs)
	return LookoutSession.objects.create(
		project=project,
		owner=owner if owner is not None else project.owner,
		journal=journal,
		**defaults,
	)


def make_journal(project, ship=None, time_spent=60, **kwargs):
	"""Create a journal entry whose time comes from an attached timelapse.

	`time_spent` is in minutes and is realised as a Lookout session, since
	journals have no self-reported time of their own.
	"""
	defaults = {
		"title": "Journal entry",
		"image_url": "https://example.com/image.png",
		"model_url": "https://example.com/model.stl",
	}
	defaults.update(kwargs)
	journal = Journal.objects.create(project=project, ship=ship, **defaults)
	if time_spent:
		make_timelapse(project, journal=journal, minutes=time_spent)
	return journal


def approve_timelapse(journal, reviewer=None, removals=(), internal_notes="looks legit"):
	"""Sign a journal off in the internal timelapse review queue.

	`removals` is (session, start_seconds, end_seconds[, reason]) tuples. Cutting
	time here is how a journal's approved hours end up below its tracked ones.
	"""
	if reviewer is None:
		reviewer = User.objects.filter(username="timelapse-reviewer").first() or make_user(
			"timelapse-reviewer", slack_id="U0TLREV"
		)
	review = TimelapseReview.objects.create(
		journal=journal, reviewer=reviewer, internal_notes=internal_notes
	)
	for removal in removals:
		session, start, end, *rest = removal
		TimelapseRemoval.objects.create(
			review=review,
			session=session,
			start_seconds=start,
			end_seconds=end,
			reason=rest[0] if rest else "afk",
		)
	return review


def make_ship(project, status=Ship.ShipStatus.T1_QUEUE, journal_minutes=(120, 120), timelapse_approved=True):
	"""Create a ship in the given status with journals attached to it.

	Its journals are signed off in timelapse review by default: that's the state
	a ship has to be in to show up in the regular review queues at all.
	"""
	ship = Ship.objects.create(project=project, status=status)
	for minutes in journal_minutes:
		journal = make_journal(project, ship=ship, time_spent=minutes)
		if timelapse_approved:
			approve_timelapse(journal)
	return ship


def image_upload(name="test.png", fmt="PNG", size=(4, 4)):
	buf = io.BytesIO()
	Image.new("RGB", size, color=(200, 30, 30)).save(buf, format=fmt)
	return SimpleUploadedFile(name, buf.getvalue(), content_type=f"image/{fmt.lower()}")


def stl_upload(name="model.stl", content=b"solid test\nendsolid test\n"):
	return SimpleUploadedFile(name, content, content_type="model/stl")


def message_texts(response):
	return [str(m) for m in get_messages(response.wsgi_request)]


@override_settings(
	STORAGES=TEST_STORAGES,
	MEDIA_URL="/media/",
	ADDRESS_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY,
)
class BaseTestCase(TestCase):
	SLACK_DM_TARGETS = [
		"atlantis_site.views.admin.review.send_slack_dm",
		"atlantis_site.views.admin.shop.send_slack_dm",
	]
	SLACK_MESSAGE_TARGETS = [
		"atlantis_site.views.admin.review.send_slack_message",
	]
	MODEL_INFO_TARGETS = [
		"atlantis_site.views.client.projects.get_model_info",
		"atlantis_site.views.admin.review.get_model_info",
	]
	IMAGE_URL_TARGETS = [
		"atlantis_site.views.admin.shop.is_valid_image_url",
		"atlantis_site.views.admin.management.is_valid_image_url",
	]

	def setUp(self):
		super().setUp()
		self.slack_dm_mocks = {}
		for target in self.SLACK_DM_TARGETS:
			patcher = patch(target, return_value=True)
			self.slack_dm_mocks[target.rsplit(".", 2)[-2]] = patcher.start()
			self.addCleanup(patcher.stop)

		self.slack_message_mocks = {}
		for target in self.SLACK_MESSAGE_TARGETS:
			patcher = patch(target, return_value=True)
			self.slack_message_mocks[target.rsplit(".", 2)[-2]] = patcher.start()
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
