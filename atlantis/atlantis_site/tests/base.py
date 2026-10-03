import io
import itertools
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from datetime import timedelta

from cryptography.fernet import Fernet
from PIL import Image

from .. import weeks
from ..checklists import FIELD as CHECKLIST_FIELD, SHIP_CHECKLIST, T1_CHECKLIST, T3_CHECKLIST
from ..models import (
	Item, Journal, Timelapse, Profile, Project, Ship, TimelapseRemoval,
	TimelapseReview,
)

User = get_user_model()

TEST_STORAGES = {
	"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
	"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()

# Where the challenge weeks sit for a test that doesn't say otherwise: far
# enough out that `timezone.now()` is always in the prep period, so the flat
# pearl rate applies, nobody can be eliminated, and no test's result depends on
# what today's date happens to be. Tests about the challenge move it with
# `during_week` below and say which week they mean.
TEST_CHALLENGE_START = "2099-01-05"  # a Monday


def during_week(index=1):
	"""override_settings putting `timezone.now()` inside challenge week `index`.

	Anchored on this week's Monday in the program's own timezone, so the run is
	always somewhere in the middle of the named week whatever day it is.
	"""
	today = timezone.now().astimezone(weeks.zone()).date()
	monday = today - timedelta(days=today.weekday())
	return override_settings(
		CHALLENGE_START_DATE=(monday - timedelta(weeks=index - 1)).isoformat()
	)


def after_week(index):
	"""override_settings putting `now` just past the end of week `index`.

	The program is `index` weeks long here, so week `index` has closed and there
	is no live week — which is what judging a finished week needs.
	"""
	return override_settings(
		**{**during_week(index + 1).options, "CHALLENGE_WEEKS": index}
	)


def in_week(index, minute=0):
	"""A datetime inside challenge week `index`, for a recording's recorded_at."""
	start, _end = weeks.week_bounds(index)
	return start + timedelta(days=2, hours=9, minutes=minute)


def before_the_program():
	"""A datetime in the prep period, which pays the flat rate."""
	return weeks.starts_at() - timedelta(days=3)


def shop_items():
	"""Items that are actually merchandise.

	A data migration seeds one hidden row per printer so a claim can become an
	ordinary order, so "how many items exist" is never zero any more. Anything
	counting what a shop action created means this.
	"""
	return Item.objects.exclude(kind=Item.Kind.PRINTER)

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


def make_timelapse(project, journal=None, minutes=60, owner=None, source=None, **kwargs):
	"""Create a finished recording — the only source of tracked time.

	Defaults to Lapse, which is how time is logged now. Pass
	`source=Timelapse.Source.LOOKOUT` (or use make_lookout) for the legacy
	recorder; the two need different identifying columns filled in, which is
	what this sorts out so callers don't have to.
	"""
	n = next(_timelapse_seq)
	source = source or Timelapse.Source.LAPSE
	if source == Timelapse.Source.LOOKOUT:
		defaults = {
			"session_id": f"session-{n}",
			"token": f"token-{n}",
		}
	else:
		defaults = {
			"lapse_id": f"lapse-{n}",
			"name": f"Timelapse {n}",
			"playback_url": f"https://cdn.example.com/lapse/{n}.mp4",
			"lapse_thumbnail_url": f"https://cdn.example.com/lapse/{n}.jpg",
		}
	defaults.update({
		"source": source,
		# Both arrive complete: a Lapse row is only ever written at the attach,
		# and a Lookout row has to have finished to be attachable.
		"status": Timelapse.Status.COMPLETE,
		"tracked_seconds": minutes * 60,
		# Set on both, the way both real code paths do — see the comment on
		# Timelapse.recorded_at for why leaving it null would misorder things.
		"recorded_at": timezone.now(),
	})
	defaults.update(kwargs)
	return Timelapse.objects.create(
		project=project,
		owner=owner if owner is not None else project.owner,
		journal=journal,
		**defaults,
	)


def make_lookout(project, journal=None, minutes=60, owner=None, **kwargs):
	"""Create a legacy Lookout session."""
	return make_timelapse(
		project, journal=journal, minutes=minutes, owner=owner,
		source=Timelapse.Source.LOOKOUT, **kwargs,
	)


def make_journal(project, ship=None, time_spent=60, **kwargs):
	"""Create a journal entry whose time comes from an attached timelapse.

	`time_spent` is in minutes and is realised as a Lapse timelapse, since
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


def ticked(checklist):
	"""POST data with every box on `checklist` ticked.

	Every checklist is refused unless all of them are, so almost every test
	that ships or approves needs this; a test about the checklist itself sends
	its own subset instead.
	"""
	return {CHECKLIST_FIELD: [item["key"] for item in checklist]}


def ship_checklist():
	return ticked(SHIP_CHECKLIST)


def t1_checklist():
	return ticked(T1_CHECKLIST)


def t3_checklist():
	return ticked(T3_CHECKLIST)


def image_upload(name="test.png", fmt="PNG", size=(4, 4)):
	buf = io.BytesIO()
	Image.new("RGB", size, color=(200, 30, 30)).save(buf, format=fmt)
	return SimpleUploadedFile(name, buf.getvalue(), content_type=f"image/{fmt.lower()}")


def stl_upload(name="model.stl", content=b"solid test\nendsolid test\n"):
	return SimpleUploadedFile(name, content, content_type="model/stl")


def message_texts(response):
	return [str(m) for m in get_messages(response.wsgi_request)]


TEST_HCA_ADDRESS = {
	"id": "adr_test",
	"line_1": "15 Falls Rd",
	"city": "Shelburne",
	"state": "VT",
	"postal_code": "05482",
	"country": "US",
	"primary": True,
}


@override_settings(
	STORAGES=TEST_STORAGES,
	MEDIA_URL="/media/",
	ADDRESS_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY,
	CHALLENGE_START_DATE=TEST_CHALLENGE_START,
)
class BaseTestCase(TestCase):
	# Keyed explicitly rather than derived from the last-but-one path segment:
	# the admin and client shop modules both end in "shop", and deriving would
	# have the second silently replace the first in the dict.
	SLACK_DM_TARGETS = {
		"review": "atlantis_site.views.admin.review.send_slack_dm",
		"shop": "atlantis_site.views.admin.shop.send_slack_dm",
		"client_shop": "atlantis_site.views.client.shop.send_slack_dm",
	}
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
		for key, target in self.SLACK_DM_TARGETS.items():
			patcher = patch(target, return_value=True)
			self.slack_dm_mocks[key] = patcher.start()
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

		# Creating and shipping both check HCA for an address; every user here
		# has one unless a test says otherwise.
		patcher = patch(
			"atlantis_site.views.helpers.fetch_addresses",
			return_value=[dict(TEST_HCA_ADDRESS)],
		)
		self.fetch_addresses_mock = patcher.start()
		self.addCleanup(patcher.stop)

		self.image_url_mocks = {}
		for target in self.IMAGE_URL_TARGETS:
			patcher = patch(target, return_value=True)
			self.image_url_mocks[target.rsplit(".", 2)[-2]] = patcher.start()
			self.addCleanup(patcher.stop)
