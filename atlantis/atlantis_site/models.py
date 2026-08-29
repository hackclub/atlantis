import os
from decimal import Decimal
from urllib.parse import urlparse

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.contrib.auth.models import User
from django.conf import settings
from django.urls import reverse


# What an hour of approved work is worth before the T3 reviewer's multiplier.
# Decimal, like everything else in the payout arithmetic: the rate is exact in
# tenths and a binary float would put the rounding off by a hair.
PEARLS_PER_HOUR = Decimal("8")

# The T3 reviewer's pearl multiplier, a Decimal in tenths so the slider's
# positions and the payout arithmetic stay exact — payouts are already in
# tenths, and a binary float would put both off by a hair.
PAYOUT_MULTIPLIER_MIN = Decimal("0.5")
PAYOUT_MULTIPLIER_MAX = Decimal("3.0")
PAYOUT_MULTIPLIER_STEP = Decimal("0.1")
PAYOUT_MULTIPLIER_DEFAULT = Decimal("1.0")


def media_url(value):
	if not value:
		return ""
	if value.startswith(("http://", "https://")):
		return value
	return reverse("serve_media", args=[value])

ALLOWED_EDITORS = [
	"Fusion 360",
	"Onshape",
	"Solidworks",
	"FreeCAD",
]

EDITOR_FILE_EXTENSIONS = {
	".f3d": "Fusion 360",
	".f3z": "Fusion 360",
	".sldprt": "Solidworks",
	".sldasm": "Solidworks",
	".slddrw": "Solidworks",
	".fcstd": "FreeCAD",
}

EDITOR_LINK_DOMAINS = {
	"onshape.com": "Onshape",
	"a360.co": "Fusion 360",
	"autodesk360.com": "Fusion 360",
}

def detect_editor_from_filename(filename):
	ext = os.path.splitext(filename)[1].lower()
	return EDITOR_FILE_EXTENSIONS.get(ext)

def detect_editor_from_link(url):
	host = (urlparse(url).netloc or "").lower()
	for domain, editor in EDITOR_LINK_DOMAINS.items():
		if host == domain or host.endswith("." + domain):
			return editor
	return None

def detect_editor(value):
	if not value:
		return None
	ext = os.path.splitext(urlparse(value).path)[1].lower()
	if ext in EDITOR_FILE_EXTENSIONS:
		return EDITOR_FILE_EXTENSIONS[ext]
	return detect_editor_from_link(value)


# Timecodes. Timelapse reviewers cut time out of a Lookout by naming a range of
# it ("0:05-0:30"), so these are the two halves of that: what a reviewer types
# and what we show back.
def format_timecode(seconds):
	"""Seconds as h:mm:ss, or m:ss when it's under an hour."""
	seconds = int(seconds)
	hours, remainder = divmod(seconds, 3600)
	minutes, secs = divmod(remainder, 60)
	if hours:
		return f"{hours}:{minutes:02d}:{secs:02d}"
	return f"{minutes}:{secs:02d}"


def parse_timecode(value):
	"""Parse "h:mm:ss", "m:ss", or a bare second count into seconds.

	Returns None for anything else rather than a best guess: a misread range
	silently removes the wrong stretch of somebody's time, so the caller has to
	be told it couldn't be read.
	"""
	if value is None:
		return None
	parts = [part.strip() for part in str(value).strip().split(":")]
	if not 1 <= len(parts) <= 3:
		return None
	if not all(part.isascii() and part.isdigit() for part in parts):
		return None
	numbers = [int(part) for part in parts]
	# Only the leading field may run past its unit, so "90:00" is 90 minutes but
	# "1:90" is not a time.
	if any(number > 59 for number in numbers[1:]):
		return None
	total = 0
	for number in numbers:
		total = total * 60 + number
	return total


# Lookout stitches one recorded minute of a session into exactly one second of
# the compiled video (its worker: "every capture unit (one recorded minute)
# becomes exactly one second of output"). A reviewer scrubbing that video is
# reading a timeline sped up sixty times, so 0:56-1:11 on the player is fifteen
# *minutes* of tracked time, not fifteen seconds. Reviewers type video offsets,
# because that is all they can see; we store tracked ones, because that is what
# comes off the shipper's hours.
TRACKED_SECONDS_PER_VIDEO_SECOND = 60


def video_to_tracked(video_seconds):
	"""An offset read off the compiled video, as tracked seconds."""
	return int(video_seconds) * TRACKED_SECONDS_PER_VIDEO_SECOND


def tracked_to_video(tracked_seconds):
	"""Tracked seconds, as an offset into the compiled video.

	Rounded up: a trailing part-minute of tracking still occupies a whole
	second of video, and a length that rounded down would claim the video ends
	before the footage does.
	"""
	tracked_seconds = max(int(tracked_seconds), 0)
	return -(-tracked_seconds // TRACKED_SECONDS_PER_VIDEO_SECOND)


def first_overlap(ranges):
	"""The first (start, end) in `ranges` that overlaps an earlier one, else None.

	Ranges are half-open — one ending at 0:30 and the next starting at 0:30 are
	adjacent, not overlapping.
	"""
	previous_end = None
	for start, end in sorted(ranges):
		if previous_end is not None and start < previous_end:
			return (start, end)
		previous_end = end
	return None


# auth model
class Profile(models.Model):
	user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="hackclub_profile")
	verification_status = models.CharField(max_length=64, blank=True, default="")
	# HCA's YSWS-eligibility verdict, which arrives alongside verification_status
	# on the same claim scope. Null while HCA has no verdict to give: an identity
	# with nothing submitted is neither eligible nor ineligible yet.
	ysws_eligible = models.BooleanField(null=True, default=None)
	slack_id = models.CharField(max_length=64, blank=True, default="")
	slack_username = models.CharField(max_length=64, blank=True, default="")
	slack_pfp_url = models.CharField(max_length=200, blank=True, default="")
	layers = models.IntegerField(default=0)
	# Encrypted (Fernet) JSON blob of the user's HCA OAuth token. Addresses are
	# never stored: this token is what buys us one from HCA on demand, on an
	# explicit "View Address".
	encrypted_hca_token = models.TextField(blank=True, default="")

	def __str__(self):
		return self.user.username

	def get_hca_token(self):
		from .crypto import decrypt_token
		return decrypt_token(self.encrypted_hca_token)

	def save_hca_token(self, token):
		"""Persist a token response, keeping the write to this column alone so
		a refresh mid-request cannot clobber unrelated in-memory changes."""
		from .crypto import encrypt_token
		from .hca import storable_token
		self.encrypted_hca_token = encrypt_token(storable_token(token))
		if self.pk:
			Profile.objects.filter(pk=self.pk).update(
				encrypted_hca_token=self.encrypted_hca_token
			)

	@property
	def is_ysws_eligible(self):
		"""True when HCA last said this user is verified and YSWS-eligible.

		A verified identity with no verdict on file is let through: HCA fills the
		flag in whenever it decides one way or the other, and only a definite "no"
		should close the door. An unverified identity — including one we were
		never told about — is not eligible.
		"""
		from .hca import VERIFICATION_VERIFIED
		return (
			self.verification_status == VERIFICATION_VERIFIED
			and self.ysws_eligible is not False
		)

	def save_verification(self, status, eligible):
		"""Persist what HCA said about verification, keeping the write to those
		two columns alone so a refresh mid-request cannot clobber unrelated
		in-memory changes."""
		self.verification_status = status
		self.ysws_eligible = eligible
		if self.pk:
			Profile.objects.filter(pk=self.pk).update(
				verification_status=status, ysws_eligible=eligible
			)

	def get_addresses(self):
		"""Fetch the user's addresses from HCA. Raises AddressUnavailable if
		their token is missing or HCA cannot be reached."""
		from .hca import fetch_addresses
		return fetch_addresses(self)

	def get_address(self, address_id=None):
		"""Return the address matching address_id, else the primary, else the
		first available address (or None)."""
		from .hca import select_address
		return select_address(self.get_addresses(), address_id)

	@property
	def primary_address_id(self):
		address = self.get_address()
		return address.get("id", "") if address else ""

# project/ship models
class Project(models.Model):
	owner = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="projects"
	)
	title = models.CharField(max_length=60, default="My Project")
	description = models.CharField(max_length=1000)
	printablesUrl = models.CharField(max_length=150, blank=True)
	editor_model_url = models.CharField(max_length=2048, blank=True)
	# Screenshot of the model, shown on the project's book cover. Required to ship.
	image_url = models.CharField(max_length=2048, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	locked = models.BooleanField(default=False)
	deleted = models.BooleanField(default=False)
	followers = models.ManyToManyField(
		settings.AUTH_USER_MODEL,
		related_name="followed_projects",
		blank=True,
	)

	def __str__(self):
		return f"{self.id}: {self.title}"

	@property
	def editor_name(self):
		return detect_editor(self.editor_model_url)

	@property
	def editor_model_display_url(self):
		return media_url(self.editor_model_url)

	@property
	def image_display_url(self):
		return media_url(self.image_url)
	
class Ship(models.Model):
	project = models.ForeignKey(
		Project,
		on_delete=models.CASCADE,
		related_name="ships"
	)
	created_at = models.DateTimeField(auto_now_add=True)
	class ShipStatus(models.TextChoices):
		REJECTED = "R", "Rejected"
		T1_QUEUE = "T1", "Under T1 Review"
		T2_QUEUE = "T2", "Under T2 Review"
		T3_QUEUE = "T3", "Under fraud review"
		FINALIZED = "F", "Finalized"
		
	status = models.CharField(
		max_length=2,
		choices=ShipStatus.choices,
		default=ShipStatus.T1_QUEUE,
	)

	def __str__(self):
		return f"Ship created at {self.created_at} with status {self.status}"

	@property
	def timelapse_cleared(self):
		"""True once every journal on this ship has passed timelapse review.

		A ship that hasn't is held out of the T1 queue, silently: shipping still
		succeeds and the owner still sees "Under T1 Review", because timelapse
		review is internal and never surfaces to them.
		"""
		return not self.journals.filter(timelapse_review__isnull=True).exists()

class T1(models.Model):
	ship = models.ForeignKey(
		Ship,
		on_delete=models.CASCADE,
		related_name="t1_reviews"
	)
	reviewer = models.ForeignKey(
		User,
		on_delete=models.PROTECT,
		related_name="t1_reviews"
	)

	reviewed_at = models.DateTimeField(auto_now_add=True)
	feedback = models.CharField(max_length=1000)
	internal_notes = models.CharField(max_length=1000)
	approved = models.BooleanField()

class T2(models.Model):
	ship = models.ForeignKey(
		Ship,
		on_delete=models.CASCADE,
		related_name="t2_reviews"
	)
	reviewer = models.ForeignKey(
		User,
		on_delete=models.PROTECT,
		related_name="t2_reviews"
	)
	class Decision(models.TextChoices):
		RETURN_T1 = "T1", "Returned to T1 Review"
		APPROVE = "A", "Approved"

	reviewed_at = models.DateTimeField(auto_now_add=True)
	decision = models.CharField(
		max_length=2,
		choices=Decision.choices,
		default=Decision.APPROVE
	)

	deductions = models.IntegerField(default=0)

	feedback = models.CharField(max_length=1000)
	justification = models.CharField(max_length=1000)

class T3(models.Model):
	ship = models.ForeignKey(
		Ship,
		on_delete=models.CASCADE,
		related_name="t3_reviews"
	)
	reviewer = models.ForeignKey(
		User,
		on_delete=models.PROTECT,
		related_name="t3_reviews"
	)

	class Decision(models.TextChoices):
		RETURN_T1 = "T1", "Returned to T1 Review"
		RETURN_T2 = "T2", "Returned to T2 Review"
		APPROVE = "A", "Approved"

	reviewed_at = models.DateTimeField(auto_now_add=True)
	decision = models.CharField(
		max_length=2	,
		choices=Decision.choices,
		default=Decision.APPROVE,
	)

	payout_time = models.IntegerField()
	airtable_time = models.IntegerField()

	# Scales the pearls paid when the ship is finalized, and nothing else:
	# payout_time and airtable_time record how long the work actually took,
	# and airtable_time is what Airtable is told.
	payout_multiplier = models.DecimalField(
		max_digits=2,
		decimal_places=1,
		default=PAYOUT_MULTIPLIER_DEFAULT,
		validators=[
			MinValueValidator(PAYOUT_MULTIPLIER_MIN),
			MaxValueValidator(PAYOUT_MULTIPLIER_MAX),
		],
	)

	internal_notes = models.CharField(blank=True)

class AirtableSubmission(models.Model):
	"""The one Airtable record a finalized ship gets, and how it went.

	The row exists so a retried finalization cannot create a second record in
	HQ's table: it is claimed (by the OneToOne, in the database) before the POST
	goes out, and a ship that already has a record_id is never submitted again.

	Nothing the shipper gave HCA is kept here. Their address and birthday are
	fetched at submission time, forwarded, and dropped — same rule as everywhere
	else. `notes` is for what was *missing* ("no address on file"), never for
	what was found.
	"""
	class Status(models.TextChoices):
		PENDING = "pending", "Not yet submitted"
		SENDING = "sending", "Submission in flight"
		SUBMITTED = "submitted", "Submitted"
		FAILED = "failed", "Failed"

	ship = models.OneToOneField(
		Ship,
		on_delete=models.CASCADE,
		related_name="airtable_submission"
	)

	status = models.CharField(
		max_length=16,
		choices=Status.choices,
		default=Status.PENDING,
	)
	# Airtable's id for the row we created ("rec..."). Its presence, not the
	# status, is the authoritative "this ship has been submitted".
	record_id = models.CharField(max_length=64, blank=True, default="")
	error = models.TextField(blank=True, default="")
	notes = models.TextField(blank=True, default="")
	attempts = models.PositiveIntegerField(default=0)

	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)
	submitted_at = models.DateTimeField(null=True, blank=True)

	class Meta:
		ordering = ["-created_at"]

	def __str__(self):
		return f"Airtable {self.get_status_display().lower()} for ship {self.ship_id}"

	@property
	def is_submitted(self):
		return bool(self.record_id)

	@property
	def needs_retry(self):
		"""Whether another attempt is safe.

		SENDING is deliberately excluded: a row stuck there means a POST went out
		and we never learned its fate, so retrying it is exactly how a duplicate
		record gets created. Those want a human to look in Airtable.
		"""
		return not self.record_id and self.status in (
			self.Status.PENDING, self.Status.FAILED
		)

	@property
	def record_url(self):
		if not self.record_id:
			return ""
		base = getattr(settings, "AIRTABLE_BASE_ID", "")
		table = getattr(settings, "AIRTABLE_TABLE_ID", "")
		if not (base and table):
			return ""
		return f"https://airtable.com/{base}/{table}/{self.record_id}"

class InternalComment(models.Model):
	ship = models.ForeignKey(
		Ship,
		on_delete=models.CASCADE,
		related_name="internal_comments"
	)
	author = models.ForeignKey(
		User,
		on_delete=models.PROTECT,
		related_name="internal_comments"
	)

	created_at = models.DateTimeField(auto_now_add=True)
	text = models.CharField(max_length=1000)

	class Meta:
		ordering = ["-created_at"]

	def __str__(self):
		return f"Internal comment on ship {self.ship_id} by {self.author_id}"

class Journal(models.Model):
	project = models.ForeignKey(
		Project,
		on_delete=models.CASCADE,
		related_name="journals"
	)

	ship = models.ForeignKey(
		Ship,
		on_delete=models.PROTECT,
		related_name="journals",
		null=True
	)

	created_at = models.DateTimeField(auto_now_add=True)
	title = models.CharField(max_length=100)
	image_url = models.CharField(max_length=2048)
	model_url = models.CharField(max_length=2048)

	@property
	def image_display_url(self):
		return media_url(self.image_url)

	@property
	def model_display_url(self):
		return media_url(self.model_url)

	@property
	def tracked_seconds(self):
		return self.timelapses.aggregate(total=models.Sum("tracked_seconds"))["total"] or 0

	@property
	def tracked_minutes(self):
		return self.tracked_seconds // 60

	@property
	def tracked_display(self):
		minutes = self.tracked_minutes
		return f"{minutes // 60}h {minutes % 60}m"

	# Everything below is the internal view of this entry's time: what a
	# timelapse reviewer took off it and what's left to pay for. None of it is
	# rendered on a page the owner can reach — they only ever see tracked_*.
	@property
	def timelapse_review_or_none(self):
		try:
			return self.timelapse_review
		except TimelapseReview.DoesNotExist:
			return None

	@property
	def timelapse_reviewed(self):
		return self.timelapse_review_or_none is not None

	@property
	def removed_seconds(self):
		review = self.timelapse_review_or_none
		return review.removed_seconds if review else 0

	@property
	def approved_seconds(self):
		return max(self.tracked_seconds - self.removed_seconds, 0)

	@property
	def approved_minutes(self):
		return self.approved_seconds // 60

	@property
	def approved_display(self):
		minutes = self.approved_minutes
		return f"{minutes // 60}h {minutes % 60}m"

	@property
	def removed_display(self):
		minutes = self.removed_seconds // 60
		return f"{minutes // 60}h {minutes % 60}m"

# lookout timelapse recording sessions
class LookoutSession(models.Model):
	class Status(models.TextChoices):
		PENDING = "pending", "Pending"
		ACTIVE = "active", "Active"
		PAUSED = "paused", "Paused"
		STOPPED = "stopped", "Stopped"
		COMPILING = "compiling", "Compiling"
		COMPLETE = "complete", "Complete"
		FAILED = "failed", "Failed"

	project = models.ForeignKey(
		Project,
		on_delete=models.CASCADE,
		related_name="timelapses"
	)
	owner = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="timelapses"
	)
	journal = models.ForeignKey(
		Journal,
		on_delete=models.SET_NULL,
		related_name="timelapses",
		null=True,
		blank=True
	)

	session_id = models.CharField(max_length=64, unique=True)
	token = models.CharField(max_length=128, unique=True)

	status = models.CharField(
		max_length=16,
		choices=Status.choices,
		default=Status.PENDING,
	)

	tracked_seconds = models.IntegerField(default=0)
	total_active_seconds = models.IntegerField(default=0)
	screenshot_count = models.IntegerField(default=0)

	heartbeats_forwarded = models.BooleanField(default=False)

	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["-created_at"]

	def __str__(self):
		return f"Timelapse {self.session_id} ({self.status}) for project {self.project_id}"

	@property
	def is_recordable(self):
		return self.status in (
			self.Status.PENDING,
			self.Status.ACTIVE,
			self.Status.PAUSED,
		)

	@property
	def is_complete(self):
		return self.status == self.Status.COMPLETE

	@property
	def is_processing(self):
		"""Recording is over but Lookout hasn't produced the video yet."""
		return self.status in (self.Status.STOPPED, self.Status.COMPILING)

	@property
	def is_attachable(self):
		return self.is_complete and self.journal_id is None

	@property
	def tracked_display(self):
		total = self.tracked_seconds or 0
		return f"{total // 3600}h {(total % 3600) // 60}m"

	@property
	def video_url(self):
		base = settings.LOOKOUT_BASE_URL.rstrip("/")
		return f"{base}/api/media/{self.session_id}/video.mp4"

	@property
	def thumbnail_url(self):
		base = settings.LOOKOUT_BASE_URL.rstrip("/")
		return f"{base}/api/media/{self.session_id}/thumbnail.jpg"

	@property
	def video_seconds(self):
		"""How long the compiled video runs, in its own sped-up timeline.

		This is the timeline a timelapse reviewer's ranges are read in — see
		TRACKED_SECONDS_PER_VIDEO_SECOND.
		"""
		return tracked_to_video(self.tracked_seconds or 0)

	@property
	def video_duration_display(self):
		return format_timecode(self.video_seconds)

	@property
	def removed_seconds(self):
		return sum(removal.deducted_seconds for removal in self.removals.all())

	@property
	def removed_display(self):
		return format_timecode(self.removed_seconds)

	@property
	def approved_seconds(self):
		return max(self.tracked_seconds - self.removed_seconds, 0)

	@property
	def approved_display(self):
		total = self.approved_seconds
		return f"{total // 3600}h {(total % 3600) // 60}m"


# internal timelapse review
class TimelapseReview(models.Model):
	"""One reviewer's pass over the Lookout footage attached to a journal.

	Strictly internal. Nothing here reaches the project owner: no notification
	is sent, no page they can load renders it, and the time the reviewer cuts
	comes off the journal quietly. There is one review per journal and it is
	never edited — it is written in a single transaction with its removals, so
	the row and its children are also the audit trail.
	"""
	journal = models.OneToOneField(
		Journal,
		on_delete=models.CASCADE,
		related_name="timelapse_review"
	)
	reviewer = models.ForeignKey(
		User,
		on_delete=models.PROTECT,
		related_name="timelapse_reviews"
	)

	reviewed_at = models.DateTimeField(auto_now_add=True)
	internal_notes = models.CharField(max_length=1000)

	class Meta:
		ordering = ["-reviewed_at"]

	def __str__(self):
		return f"Timelapse review of journal {self.journal_id} by {self.reviewer_id}"

	@property
	def removed_seconds(self):
		return sum(removal.deducted_seconds for removal in self.removals.all())

	@property
	def removed_minutes(self):
		return self.removed_seconds // 60

	@property
	def removed_display(self):
		minutes = self.removed_minutes
		return f"{minutes // 60}h {minutes % 60}m"


class TimelapseRemoval(models.Model):
	"""A stretch of one Lookout session the reviewer refused to pay for.

	Offsets are into the session's tracked timeline, not into the compiled
	video the reviewer read them off: the video runs sixty times faster (see
	TRACKED_SECONDS_PER_VIDEO_SECOND), and it is tracked seconds the deduction
	is made of. The view converts what was typed on its way in, and
	video_range_display converts back for anyone re-checking against the
	player. Capping end_seconds at the session's tracked_seconds (enforced by
	the view) is what keeps an adjusted duration from going negative.
	"""
	review = models.ForeignKey(
		TimelapseReview,
		on_delete=models.CASCADE,
		related_name="removals"
	)
	session = models.ForeignKey(
		LookoutSession,
		on_delete=models.CASCADE,
		related_name="removals"
	)

	start_seconds = models.PositiveIntegerField()
	end_seconds = models.PositiveIntegerField()
	# 100 preserves the original all-or-nothing removal behavior. Fallout
	# deflations store the percentage of the range that is deducted.
	deduction_percent = models.PositiveSmallIntegerField(
		default=100,
		validators=[MinValueValidator(0), MaxValueValidator(100)],
	)
	# Required, per range: a deduction nobody can explain later is indefensible.
	reason = models.CharField(max_length=1000)

	class Meta:
		ordering = ["session_id", "start_seconds"]
		constraints = [
			models.CheckConstraint(
				condition=models.Q(end_seconds__gt=models.F("start_seconds")),
				name="timelapse_removal_end_after_start",
			),
			models.CheckConstraint(
				condition=~models.Q(reason=""),
				name="timelapse_removal_reason_required",
			),
			models.CheckConstraint(
				condition=models.Q(deduction_percent__lte=100),
				name="timelapse_removal_deduction_percent_valid",
			),
		]

	def __str__(self):
		return f"{self.range_display} removed from session {self.session_id}"

	@property
	def duration_seconds(self):
		return max(self.end_seconds - self.start_seconds, 0)

	@property
	def deducted_seconds(self):
		return round(self.duration_seconds * self.deduction_percent / 100)

	@property
	def duration_display(self):
		return format_timecode(self.duration_seconds)

	@property
	def range_display(self):
		return f"{format_timecode(self.start_seconds)}-{format_timecode(self.end_seconds)}"

	@property
	def video_range_display(self):
		"""The same stretch as timecodes on the compiled video's own timeline.

		What the reviewer typed, and what anyone re-checking the call scrubs
		to. The end rounds up, so a range clamped to the end of a session's
		tracked time still points at the last second of footage.
		"""
		start = tracked_to_video(self.start_seconds)
		end = tracked_to_video(self.end_seconds)
		return f"{format_timecode(start)}-{format_timecode(end)}"


# shop models
class ShopCategory(models.Model):
	"""Where a category's shelf sits on the shop page.

	Item.category stays free text, so a row here is only ever about ordering:
	one is created the first time an admin uses a category name, and admins drag
	them into the order shoppers see. A category with no row (an item edited
	straight through the Django admin, say) falls to the bottom of the shop.
	"""

	name = models.CharField(max_length=40, unique=True)
	sort_order = models.PositiveIntegerField(default=0)

	class Meta:
		ordering = ["sort_order", "name"]
		verbose_name_plural = "shop categories"

	def __str__(self):
		return f"{self.name} (#{self.sort_order})"

	@classmethod
	def ensure(cls, name):
		"""Give a category name a place in the order the first time it is used."""
		category = cls.objects.filter(name=name).first()
		if category:
			return category
		last = cls.objects.aggregate(last=models.Max("sort_order"))["last"] or 0
		category, _ = cls.objects.get_or_create(name=name, defaults={"sort_order": last + 1})
		return category

	@classmethod
	def order_items(cls, queryset):
		"""Sort items so the shelves come out in the admin-set category order."""
		sort_order = cls.objects.filter(name=models.OuterRef("category")).values("sort_order")[:1]
		return queryset.annotate(category_order=models.Subquery(sort_order)).order_by(
			models.F("category_order").asc(nulls_last=True), "category", "id"
		)


class Item(models.Model):
	name = models.CharField(max_length=60)
	description = models.CharField(max_length=100)
	cost = models.PositiveIntegerField()
	deleted = models.BooleanField(default=False)
	imageUrl = models.URLField(max_length=2048, default="https://example.com")
	category = models.CharField(max_length=40, default="Other")
	stock = models.IntegerField(
		default=-1,
		help_text="Units available to order. -1 means unlimited stock.",
	)

	@property
	def unlimited_stock(self):
		return self.stock < 0

	@property
	def in_stock(self):
		return self.unlimited_stock or self.stock > 0

	def __str__(self):
		return f"{self.name} ({self.description}) for {self.cost} layers"
	
class Order(models.Model):
	owner = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="orders"
	)
	item = models.ForeignKey(
		Item,
		on_delete=models.PROTECT,
		related_name="orders"
	)
	fulfiller = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.PROTECT,
		related_name="orders_fulfilled",
		null=True,
		blank=True
	)

	class OrderStatus(models.TextChoices):
		PENDING = "P", "Pending"
		FULFILLED = "F", "Fulfilled"
		DENIED = "D", "Denied"
		REFUNDED = "R", "Refunded"
	
	status = models.CharField(
		max_length=1,
		choices=OrderStatus.choices,
		default=OrderStatus.PENDING,
	)

	admin_notes = models.CharField(max_length=100, blank=True)
	user_notes = models.CharField(max_length=100, blank=True)

	address_id = models.CharField(max_length=20, blank=True)
	fulfilled_at = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	quantity = models.PositiveIntegerField(default=1)
	cost = models.PositiveIntegerField(blank=True)
	refunded = models.BooleanField(blank=True, null=True)

	def save(self, *args, **kwargs):
		if self.cost is None and self.item:
			self.cost = self.item.cost
		super().save(*args, **kwargs)

class AuditLog(models.Model):
	actor = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		related_name="audit_logs",
		null=True,
		blank=True
	)
	action = models.CharField(max_length=64)
	target = models.CharField(max_length=255, blank=True)
	path = models.CharField(max_length=255, blank=True)
	method = models.CharField(max_length=8, blank=True)
	ip_address = models.CharField(max_length=64, blank=True)
	form_data = models.JSONField(default=dict, blank=True)
	metadata = models.JSONField(default=dict, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["-created_at"]
		indexes = [
			models.Index(fields=["-created_at"]),
			models.Index(fields=["action"]),
		]

	def __str__(self):
		who = self.actor.username if self.actor else "deleted user"
		return f"{self.created_at:%Y-%m-%d %H:%M} {who} {self.action}"

# permissions model
class Permissions(models.Model):
	class Meta:
		verbose_name = "Permission"
		verbose_name_plural = "Permissions"
		
		permissions = [
			("t1_review", "T1 Project Review"),
			("t2_review", "T2 Project Review"),
			("t3_review", "T3/Fraud Project Review"),
			# Deliberately its own grant rather than something a T1/T2/T3
			# reviewer picks up: timelapse review is a different job, and the
			# people who do it are not the people who talk to the shipper.
			("timelapse_review", "Timelapse Review (internal)"),
			("fulfillment", "Fulfill shop orders"),
			("organizer", "Access to everything")
		]
	
	def __str__(self):
		return "why are you stringing the permissions class doofus"