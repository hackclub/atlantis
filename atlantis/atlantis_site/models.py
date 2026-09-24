import os
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlparse

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.contrib.auth.models import User
from django.conf import settings
from django.urls import reverse
from django.utils import timezone


# What an hour of approved work is worth before the T3 reviewer's multiplier.
# Decimal, like everything else in the payout arithmetic: the rate is exact in
# tenths and a binary float would put the rounding off by a hair.
#
# This is the *prep* rate, and it is only paid on work logged before the
# challenge weeks opened. Once week 1 starts, an hour is worth one of the two
# rates below depending on whether it lands inside that week's required five
# hours or on top of them; see challenge.py, which does the splitting.
PEARLS_PER_HOUR = Decimal("8")

# Inside the weekly five: an hour that is *owed* rather than extra. It pays
# little because it is already being paid for in printer hours — those five are
# what fill the 40-hour bar.
CHALLENGE_BASE_PEARLS_PER_HOUR = Decimal("2")

# Every hour past the weekly five. Worth more than the prep rate: the required
# hours are behind you and this is the only way to afford a better printer.
CHALLENGE_BONUS_PEARLS_PER_HOUR = Decimal("7")

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

# Every editor can also export a project as an archive, and some (Onshape,
# multi-part Solidworks assemblies) only really travel that way. An archive
# says "this is a source file" without saying which editor made it, so it maps
# to no editor name — detect_editor stays honest and returns None for these.
EDITOR_ARCHIVE_EXTENSIONS = {
	".zip",
}

# Taken on upload without being advertised. These stay out of ALLOWED_EDITORS
# and out of every string built from it, so no copy on the page names them;
# they only reach the file picker's accept list, which is what makes the file
# selectable at all. Like an archive, they map to no editor name.
UNLISTED_EDITOR_EXTENSIONS = {
	".shapr",
}

EDITOR_LINK_DOMAINS = {
	"onshape.com": "Onshape",
	"a360.co": "Fusion 360",
	"autodesk360.com": "Fusion 360",
}

# The editors that hand out a share link at all. Solidworks and FreeCAD have no
# such domain, so telling everyone to "paste a link" and then matching only
# these would turn those two away with nowhere to go — the link field takes a
# direct link to a source file as well, and this names who the domains are for.
LINKABLE_EDITORS = sorted(set(EDITOR_LINK_DOMAINS.values()))

def detect_editor_from_filename(filename):
	ext = os.path.splitext(filename)[1].lower()
	return EDITOR_FILE_EXTENSIONS.get(ext)

def is_editor_model_file(value):
	"""Is this a filename or URL we accept as an editor source file?

	Broader than detect_editor_from_filename: archives count even though they
	don't name an editor.
	"""
	if not value:
		return False
	path = urlparse(value).path
	ext = os.path.splitext(path)[1].lower()
	if ext in EDITOR_ARCHIVE_EXTENSIONS or ext in UNLISTED_EDITOR_EXTENSIONS:
		return True
	return detect_editor_from_filename(path) is not None

def detect_editor_from_link(url):
	host = (urlparse(url).netloc or "").lower()
	for domain, editor in EDITOR_LINK_DOMAINS.items():
		if host == domain or host.endswith("." + domain):
			return editor
	return None

def detect_editor(value):
	if not value:
		return None
	return detect_editor_from_filename(urlparse(value).path) or detect_editor_from_link(value)


# Timecodes. Timelapse reviewers cut time out of a recording by naming a range
# of it ("0:05-0:30"), so these are the two halves of that: what a reviewer types
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


# Both recorders stitch one recorded minute into exactly one second of the
# compiled video — Lookout's worker: "every capture unit (one recorded minute)
# becomes exactly one second of output", and Lapse reports a `duration` of 720
# for a video that runs twelve. A reviewer scrubbing that video is
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

	# When we last saw this user make a request, written by the presence
	# middleware (see presence.py) and read only by the metrics page. Coarse on
	# purpose: that middleware writes at most once a minute per user, so this
	# means "here within the last minute or so" rather than an exact moment.
	# Null for anyone who has not loaded a page since presence tracking landed.
	last_seen = models.DateTimeField(null=True, blank=True, db_index=True)

	# Which manufacturer's tech tree this user is working towards, as a slug
	# from printers.py, or "" for undecided. Free to change right up until the
	# claim at the end of the program, because nothing is spent before then:
	# the tree is a plan, and the one debit that pays for it happens once.
	printer_track = models.CharField(max_length=32, blank=True, default="")

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

class ActiveDay(models.Model):
	"""One row per user per day they were seen on the site.

	Profile.last_seen answers "who is here right now" and "who was here today",
	but it is a single timestamp: it cannot say how many people were around last
	Tuesday. This is that history — one row per (user, day), written by the same
	middleware and never updated afterwards — so a day's attendance is settled
	once the day is over, and an average across a window is a count rather than
	an estimate.

	Days are UTC dates, the same dates every other window on the metrics page is
	cut in. Rows are cheap (one per user per active day) and the unique
	constraint is what makes the write idempotent: presence.py inserts with
	ON CONFLICT DO NOTHING rather than reading first.
	"""
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="active_days"
	)
	day = models.DateField()

	class Meta:
		ordering = ["-day"]
		indexes = [models.Index(fields=["day"])]
		constraints = [
			models.UniqueConstraint(
				fields=["user", "day"],
				name="active_day_once_per_user",
			),
		]

	def __str__(self):
		return f"{self.user_id} seen on {self.day}"


# project/ship models
class Project(models.Model):
	owner = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="projects"
	)
	title = models.CharField(max_length=60, default="My Project")
	description = models.CharField(max_length=1000)
	printablesUrl = models.CharField(max_length=2048, blank=True)
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
		# A T1 reviewer wants something fixed before they'll decide. The ship
		# isn't over: it keeps its journals, waits on the shipper, and goes back
		# into the T1 queue as the same ship when they resubmit.
		CHANGES_REQUESTED = "C", "Changes requested"
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
	# Only ever set alongside approved=False: the reviewer sent the ship back to
	# the shipper for fixes rather than rejecting it.
	changes_requested = models.BooleanField(default=False)

	@property
	def verdict(self):
		"""What this review decided, as the review pages and Slack word it."""
		if self.approved:
			return "approved"
		if self.changes_requested:
			return "changes requested"
		return "rejected"

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

	# The pearls this decision actually credited. Stored rather than worked out
	# again later, because it no longer can be: what an hour pays depends on
	# which week it was recorded in and on how much of that week's cheap-rate
	# allowance earlier ships had already spent, so payout_time and the
	# multiplier are no longer enough to rebuild the figure. Null on rows
	# written before the split existed, where the flat rate still reconstructs
	# it exactly.
	payout_layers = models.IntegerField(null=True, blank=True)

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

# a piece of recorded footage, attached to a lapse and paid out on
class Timelapse(models.Model):
	"""One recording backing a lapse's hours, from Lapse or from Lookout.

	Two things produced these and only one still does. A Lapse timelapse is
	recorded and published on lapse.hackclub.com and taped in here afterwards,
	so its row exists only once the shipper has attached it. A Lookout session
	was recorded *by this site* — we opened it, drove it and waited for a video
	— so its row exists from the moment recording starts and moves through a
	status lifecycle. `source` says which, and it is the only thing that should
	ever be branched on.

	They share a table because everything downstream of the attach treats them
	identically: the tracked time on one is the tracked time on the other, and
	the internal timelapse review annotates and cuts time from both through the
	same TimelapseAnnotation and TimelapseRemoval rows. Splitting them would
	have meant an exclusive-arc foreign key on the audit trail that decides
	payouts, which is a bad trade for a discriminator column.

	`tracked_seconds` is the one number that turns into money, and it is never
	self-reported from either source: for Lapse it is the API's `duration`
	verbatim, re-read at attach time; for Lookout it is what Lookout's internal
	API reported. Note that Lapse's `duration` is *recorded* time and not the
	length of the compiled video — the API reports 720 for a video that runs
	twelve seconds — which is the same sixty-to-one the reviewers already read
	footage in. See TRACKED_SECONDS_PER_VIDEO_SECOND.
	"""

	class Source(models.TextChoices):
		LAPSE = "lapse", "Lapse"
		LOOKOUT = "lookout", "Lookout (legacy)"

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

	# Which recorder this came off. Defaults to Lapse because that is the only
	# one that can produce a new row through the book; a Lookout row is created
	# by the legacy recorder, which sets this explicitly.
	source = models.CharField(
		max_length=16,
		choices=Source.choices,
		default=Source.LAPSE,
	)

	# ---- Lapse ----------------------------------------------------------
	# Lapse's own id for the recording, and empty on a Lookout row. Unique
	# where set, and that constraint is load-bearing: it is what stops one
	# piece of footage being taped into two lapses and paid for twice.
	lapse_id = models.CharField(max_length=64, blank=True, default="")
	# What the shipper published it as. Carried so a reviewer and HQ see the
	# same title the shipper does, rather than an opaque id.
	name = models.CharField(max_length=120, blank=True, default="")
	# The compiled video, straight off Lapse's `playbackUrl`. Durable: it is a
	# stable URL that 302s to storage signed at request time, so it keeps
	# working long after the attach and a stored copy does not go stale.
	# Empty on a Lookout row, whose video is built from its session id instead.
	playback_url = models.URLField(max_length=500, blank=True, default="")
	lapse_thumbnail_url = models.URLField(max_length=500, blank=True, default="")
	# When the footage was recorded, which for a Lapse row is not created_at:
	# that is when it was taped in here, and the two can be days apart. A
	# Lookout row sets this to when recording started, where the two really
	# were the same moment — filled in on both so `ordering` below means one
	# thing. Postgres sorts nulls first on a descending sort, so leaving it
	# empty on half the table would file the legacy footage as the newest.
	recorded_at = models.DateTimeField(null=True, blank=True)

	# ---- Lookout (legacy) -----------------------------------------------
	# Empty on a Lapse row. `token` is a live credential to Lookout that the
	# browser recorder drives the session with; it is never rendered anywhere
	# but into that recorder's own config, and never on a page anyone else
	# can load.
	session_id = models.CharField(max_length=64, blank=True, default="")
	token = models.CharField(max_length=128, blank=True, default="")
	# Only ever moves on a Lookout row: we drove that recording, so we watched
	# it through pending -> active -> compiling -> complete. A Lapse timelapse
	# arrives finished and is written COMPLETE at the attach.
	status = models.CharField(
		max_length=16,
		choices=Status.choices,
		default=Status.PENDING,
	)
	total_active_seconds = models.IntegerField(default=0)
	screenshot_count = models.IntegerField(default=0)
	heartbeats_forwarded = models.BooleanField(default=False)

	# ---- shared ---------------------------------------------------------
	tracked_seconds = models.IntegerField(default=0)

	# What the activity checker found in the compiled video: stretches where
	# nothing on screen changed. Advisory only — it is drawn under the player
	# so a reviewer knows where to look, and it deducts nothing by itself. See
	# activity.py for how the numbers are produced.
	inactive_frame_count = models.IntegerField(default=0)
	inactive_percentage = models.FloatField(default=0.0)
	# [{"start": <video seconds>, "end": ..., "duration": ...}], in the
	# compiled video's own timeline — the one the reviewer scrubs.
	inactive_segments = models.JSONField(default=list, blank=True)
	activity_checked_at = models.DateTimeField(null=True, blank=True)
	# How long the compiled video actually runs, read off the file itself by
	# the activity check. Null until something has measured it; until then the
	# length is estimated per source (see video_seconds).
	measured_video_seconds = models.IntegerField(null=True, blank=True)

	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		# Recorded-first, so a page reads in the order the work happened rather
		# than the order it was taped in. created_at is the tiebreak, and the
		# fallback for anything Lapse gave no usable timestamp for.
		ordering = ["-recorded_at", "-created_at"]
		constraints = [
			# Conditional rather than a plain unique=True, because the column
			# is empty on every row from the other source and Postgres would
			# otherwise let exactly one of them exist.
			models.UniqueConstraint(
				fields=["lapse_id"],
				condition=~models.Q(lapse_id=""),
				name="timelapse_lapse_id_unique",
			),
			models.UniqueConstraint(
				fields=["session_id"],
				condition=~models.Q(session_id=""),
				name="timelapse_session_id_unique",
			),
			models.UniqueConstraint(
				fields=["token"],
				condition=~models.Q(token=""),
				name="timelapse_token_unique",
			),
			# A row has to be identifiable on the service it came from, or
			# nothing can re-read it later to check the hours behind it.
			models.CheckConstraint(
				condition=(
					(models.Q(source="lapse") & ~models.Q(lapse_id=""))
					| (models.Q(source="lookout") & ~models.Q(session_id=""))
				),
				name="timelapse_source_has_id",
			),
		]

	def __str__(self):
		return f"{self.get_source_display()} timelapse {self.external_id} for project {self.project_id}"

	# ---- which recorder --------------------------------------------------
	@property
	def is_lapse(self):
		return self.source == self.Source.LAPSE

	@property
	def is_lookout(self):
		return self.source == self.Source.LOOKOUT

	@property
	def external_id(self):
		"""However the service that recorded this names it."""
		return self.lapse_id if self.is_lapse else self.session_id

	# ---- lifecycle -------------------------------------------------------
	# Only a Lookout row is ever anything but complete, but these are asked of
	# both: the picker, the book and the review desk don't branch on source.
	@property
	def is_recordable(self):
		"""Still being recorded, and so resumable. Never true of a Lapse row."""
		return self.is_lookout and self.status in (
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
	def is_failed(self):
		return self.status == self.Status.FAILED

	@property
	def is_attachable(self):
		return self.is_complete and self.journal_id is None

	# ---- links -----------------------------------------------------------
	@property
	def watch_url(self):
		"""Where a person goes to watch this.

		The link that goes to anyone reading the ship later — a reviewer here,
		HQ in Airtable — so it is a page a human can open rather than a bare
		file. For Lapse that is the timelapse's permalink, which also carries
		its name, its owner and its comments; for Lookout it is the compiled
		mp4, which is all that service ever offered.
		"""
		if self.is_lapse:
			from .lapse import watch_url
			return watch_url(self.lapse_id)
		return self.video_url

	@property
	def video_url(self):
		"""The video file itself, for a <video> element or the activity pass."""
		if self.is_lapse:
			return self.playback_url
		base = settings.LOOKOUT_BASE_URL.rstrip("/")
		return f"{base}/api/media/{self.session_id}/video.mp4"

	@property
	def thumbnail_url(self):
		if self.is_lapse:
			return self.lapse_thumbnail_url
		base = settings.LOOKOUT_BASE_URL.rstrip("/")
		return f"{base}/api/media/{self.session_id}/thumbnail.jpg"

	@property
	def display_name(self):
		"""What to call this recording on a page. Lookout never named its own."""
		return self.name or f"{self.get_source_display()} recording"

	# ---- time ------------------------------------------------------------
	@property
	def tracked_display(self):
		total = self.tracked_seconds or 0
		return f"{total // 3600}h {(total % 3600) // 60}m"

	@property
	def estimated_video_seconds(self):
		"""How long the compiled video runs, going by what the service reported.

		For Lookout, one confirmed screenshot is one second of output, so the
		shot count is the length. Tracked time is not: a session can hold
		screenshots it was never credited for — a capture Lookout refused, one
		taken while the clock wasn't running, the bucket a session opens with —
		and every one of those is still a frame in the video. Deriving the
		length from tracked time alone made the page claim a video shorter than
		the one the reviewer was watching, which put the tail of it out of
		reach. The tracked-derived figure stays on as a floor, for a session
		whose shot count never synced.

		Lapse reports no shot count, and its `duration` is exactly the recorded
		time the video is a sixty-times-faster rendering of, so the conversion
		is the whole answer there.
		"""
		tracked = tracked_to_video(self.tracked_seconds or 0)
		if self.is_lapse:
			return tracked
		return max(self.screenshot_count or 0, tracked)

	@property
	def video_seconds(self):
		"""How long the compiled video runs, in its own sped-up timeline.

		This is the timeline a timelapse reviewer's ranges are read in — see
		TRACKED_SECONDS_PER_VIDEO_SECOND. Measured off the video where the
		activity check has been over it, estimated where it hasn't.
		"""
		if self.measured_video_seconds is not None:
			return self.measured_video_seconds
		return self.estimated_video_seconds

	@property
	def video_duration_display(self):
		return format_timecode(self.video_seconds)

	@property
	def removed_seconds(self):
		return self.removals.aggregate(
			total=models.Sum(
				models.F("end_seconds") - models.F("start_seconds"),
				output_field=models.IntegerField(),
			)
		)["total"] or 0

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

	@property
	def activity_checked(self):
		"""True once the inactivity pass has run over this recording's video.

		Distinct from "found nothing": an unchecked recording is drawn as
		unanalysed, a checked one with no segments is drawn as clean, and the
		reviewer is owed the difference.
		"""
		return self.activity_checked_at is not None

	@property
	def inactive_seconds(self):
		"""Video seconds the checker called inactive — one per recorded minute."""
		return sum(
			int(segment.get("duration") or 0)
			for segment in (self.inactive_segments or [])
		)

	@property
	def inactive_display(self):
		return format_timecode(video_to_tracked(self.inactive_seconds))


class LapseAccount(models.Model):
	"""A shipper's connection to their Lapse account.

	One per user, created when they come back through the authorize page. The
	token is the whole point of the row: it is what the picker reads their
	published timelapses with, and it is stored the way the HCA token is —
	encrypted at rest, never rendered, never sent anywhere but Lapse.

	There is no refresh grant on the Lapse token endpoint, so an expired token
	cannot be renewed behind the shipper's back. `is_expired` is what lets the
	book say "reconnect" before a request fails instead of after.
	"""
	user = models.OneToOneField(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="lapse_account"
	)

	# Who Lapse says the token belongs to. Shown in the picker so a shipper
	# with two accounts can see which one they connected.
	lapse_user_id = models.CharField(max_length=64, blank=True, default="")
	handle = models.CharField(max_length=64, blank=True, default="")
	display_name = models.CharField(max_length=64, blank=True, default="")
	profile_picture_url = models.CharField(max_length=300, blank=True, default="")

	# Fernet-encrypted JSON of the whole token response, same as Profile does
	# with the HCA token.
	encrypted_token = models.TextField(blank=True, default="")
	expires_at = models.DateTimeField(null=True, blank=True)
	scope = models.CharField(max_length=200, blank=True, default="")

	connected_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	def __str__(self):
		return f"Lapse account {self.handle or self.lapse_user_id} for {self.user_id}"

	def save_token(self, token):
		"""Persist a token response and when it runs out.

		The expiry comes out of the JWT wherever it has one, because that claim
		is what Lapse enforces. `expires_in` sits beside it in the same response
		and the two have been seen to disagree — a token arriving already
		expired while `expires_in` still claimed an hour — and trusting the
		wrong one leaves a shipper connected on paper and refused on every call.
		`expires_in` stays as the fallback for a token that carries no readable
		claim.

		Either way a minute is shaved off, so a token that runs out mid-request
		is treated as gone before it is used rather than failing a call the
		shipper then has to retry.
		"""
		from .crypto import encrypt_token
		from .lapse import token_expiry

		self.encrypted_token = encrypt_token(token)
		# Trimmed, not refused: the scope string is Lapse's to write and a long
		# one would fail the write rather than the authorization it belongs to.
		self.scope = (token.get("scope") or "")[:self._meta.get_field("scope").max_length]

		margin = timedelta(seconds=60)
		claimed = token_expiry(token.get("access_token", ""))
		if claimed is not None:
			self.expires_at = claimed - margin
			return

		expires_in = token.get("expires_in")
		try:
			seconds = int(expires_in)
		except (TypeError, ValueError):
			seconds = None
		self.expires_at = (
			timezone.now() + timedelta(seconds=max(seconds - 60, 0))
			if seconds is not None
			else None
		)

	def forget_token(self):
		"""Drop a credential Lapse has refused, keeping the rest of the row.

		Called when a request comes back rejected. Without it the connection
		sits there looking usable and every call fails the same way; clearing
		it is what turns the picker back into a "reconnect" prompt, which is
		the one thing that actually fixes it. The handle and the connection
		date stay, so the page can still say whose account it was.
		"""
		self.encrypted_token = ""
		self.expires_at = timezone.now()
		self.save(update_fields=["encrypted_token", "expires_at", "updated_at"])

	@property
	def access_token(self):
		from .crypto import decrypt_token
		return (decrypt_token(self.encrypted_token) or {}).get("access_token", "")

	@property
	def is_expired(self):
		return bool(self.expires_at and self.expires_at <= timezone.now())

	@property
	def is_usable(self):
		return bool(self.access_token) and not self.is_expired


# internal timelapse review
class TimelapseReview(models.Model):
	"""One reviewer's pass over the footage attached to a journal.

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
	# Optional: the per-recording descriptions carry the account of what was
	# watched, and this is the space for anything that spans the whole pass.
	internal_notes = models.CharField(max_length=1000, blank=True)

	class Meta:
		ordering = ["-reviewed_at"]

	def __str__(self):
		return f"Timelapse review of journal {self.journal_id} by {self.reviewer_id}"

	@property
	def removed_seconds(self):
		return self.removals.aggregate(
			total=models.Sum(
				models.F("end_seconds") - models.F("start_seconds"),
				output_field=models.IntegerField(),
			)
		)["total"] or 0

	@property
	def removed_minutes(self):
		return self.removed_seconds // 60

	@property
	def removed_display(self):
		minutes = self.removed_minutes
		return f"{minutes // 60}h {minutes % 60}m"


class TimelapseAnnotation(models.Model):
	"""What one reviewer wrote about one recording while signing it off.

	A sentence or two, per piece of footage, for whoever reads this ship
	downstream: what the recording shows, and whether the time in it looks
	like the work it is claimed for. The reviewer writes one before the pass
	can be submitted, which is the point — a recording nobody described is a
	recording nobody watched.

	Separate from TimelapseRemoval because a description is about a whole
	recording and a removal is about a range of one; a recording with nothing
	cut from it still gets described.
	"""
	review = models.ForeignKey(
		TimelapseReview,
		on_delete=models.CASCADE,
		related_name="annotations"
	)
	session = models.ForeignKey(
		Timelapse,
		on_delete=models.CASCADE,
		related_name="annotations"
	)

	description = models.CharField(max_length=500)

	class Meta:
		ordering = ["session_id"]
		constraints = [
			models.UniqueConstraint(
				fields=["review", "session"],
				name="timelapse_annotation_one_per_session",
			),
		]

	def __str__(self):
		return f"Description of session {self.session_id} on review {self.review_id}"


class TimelapseRemoval(models.Model):
	"""A stretch of one recording the reviewer refused to pay for.

	Offsets are into the session's tracked timeline, not into the compiled
	video the reviewer read them off: the video runs sixty times faster (see
	TRACKED_SECONDS_PER_VIDEO_SECOND), and it is tracked seconds the deduction
	is made of. The view converts what was typed on its way in, and
	video_range_display converts back for anyone re-checking against the
	player. Capping end_seconds at the session's tracked_seconds (enforced by
	the view) is what keeps an adjusted duration from going negative — along
	with the view's refusal of a range that *starts* past the tracked time,
	which the cap alone would turn into a range ending before it began.
	"""
	review = models.ForeignKey(
		TimelapseReview,
		on_delete=models.CASCADE,
		related_name="removals"
	)
	session = models.ForeignKey(
		Timelapse,
		on_delete=models.CASCADE,
		related_name="removals"
	)

	start_seconds = models.PositiveIntegerField()
	end_seconds = models.PositiveIntegerField()
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
		]

	def __str__(self):
		return f"{self.range_display} removed from session {self.session_id}"

	@property
	def duration_seconds(self):
		return max(self.end_seconds - self.start_seconds, 0)

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
# challenge models
class WeekOutcome(models.Model):
	"""What happened to one user in one challenge week, once the week is over.

	A row is the *record* of a closed week, not the source of truth for it.
	Whether a week was met is recomputed from live data on every read (see
	challenge.py): savers bought after the fact change the answer, and a row
	written at close would otherwise go stale the moment someone revived.

	What the row is actually for is the three things that can't be derived:
	`notified_at`, so the elimination DM goes out exactly once however many
	times the closer runs; `override`, so an organizer can rule on a week by
	hand; and `real_minutes`, a snapshot of the tracked time as it stood at
	close, kept for the admin history because live time can still move
	afterwards.
	"""

	class Override(models.TextChoices):
		NONE = "", "No override"
		PASS = "P", "Forced pass"
		FAIL = "F", "Forced fail"

	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="week_outcomes",
	)
	week_index = models.PositiveSmallIntegerField()

	# Tracked minutes this user logged in this week, as they stood when the
	# week closed. Savers are deliberately not folded in — they are counted
	# from SaverCredit so that buying one after the close still counts.
	real_minutes = models.PositiveIntegerField(default=0)
	passed = models.BooleanField(default=False)

	closed_at = models.DateTimeField(auto_now_add=True)
	# When the "you missed a week" DM went out. Null means it hasn't, which is
	# the only thing that lets close_week be safe to run twice.
	notified_at = models.DateTimeField(null=True, blank=True)

	override = models.CharField(
		max_length=1, choices=Override.choices, blank=True, default=Override.NONE
	)
	override_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		related_name="week_overrides",
		null=True,
		blank=True,
	)
	override_note = models.CharField(max_length=200, blank=True, default="")

	class Meta:
		ordering = ["user_id", "week_index"]
		constraints = [
			models.UniqueConstraint(
				fields=["user", "week_index"], name="one_outcome_per_user_week"
			),
		]
		indexes = [models.Index(fields=["week_index"])]

	def __str__(self):
		return f"{self.user_id} week {self.week_index}: {'met' if self.passed else 'missed'}"


class PearlBracket(models.Model):
	"""How much of one week's cheap-rate allowance a user has already been paid.

	The first five hours of a week pay the base rate and everything above them
	pays the bonus rate, but pearls are handed out at T3 finalization — one
	lump for a whole ship, weeks after the week in question, and a week's hours
	can be spread over several projects that finalize on different days. So the
	bracket cannot be worked out from the ship being finalized alone; it needs
	to know what earlier ships already drew.

	That's this: minutes of week `week_index` that have already been paid at the
	base rate, counted up as each ship finalizes. Once it reaches
	weeks.WEEKLY_MINUTES, everything further from that week pays the bonus rate,
	whichever project it was logged against.
	"""

	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="pearl_brackets",
	)
	week_index = models.PositiveSmallIntegerField()
	minutes_paid = models.PositiveIntegerField(default=0)

	class Meta:
		ordering = ["user_id", "week_index"]
		constraints = [
			models.UniqueConstraint(
				fields=["user", "week_index"], name="one_bracket_per_user_week"
			),
		]

	def __str__(self):
		return f"{self.user_id} week {self.week_index}: {self.minutes_paid}m at base rate"


class SaverCredit(models.Model):
	"""One hour credited to one week by a streak saver, append-only.

	Savers are never held: buying one applies it here and then it is history.
	That makes this the whole story of how a week got to five hours without
	five hours being logged, and keeps a revived week auditable — each row says
	which order paid for it, or which organizer granted it.

	One row per hour. Buying three at once writes three rows rather than a row
	with a quantity, so a partial refund is a deletion of rows and the count is
	never two numbers that can disagree.
	"""

	class Source(models.TextChoices):
		PURCHASE = "purchase", "Bought in the shop"
		ADMIN = "admin", "Granted by an organizer"

	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="saver_credits",
	)
	week_index = models.PositiveSmallIntegerField()
	source = models.CharField(
		max_length=16, choices=Source.choices, default=Source.PURCHASE
	)
	# The order that paid for it. Null on an organizer's grant, and SET_NULL
	# rather than CASCADE so deleting an order can never quietly un-save a week.
	order = models.ForeignKey(
		"Order",
		on_delete=models.SET_NULL,
		related_name="saver_credits",
		null=True,
		blank=True,
	)
	granted_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		related_name="savers_granted",
		null=True,
		blank=True,
	)
	note = models.CharField(max_length=200, blank=True, default="")
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["-created_at"]
		indexes = [
			models.Index(fields=["user", "week_index"]),
		]

	def __str__(self):
		return f"saver hour for {self.user_id}, week {self.week_index}"


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
	class Kind(models.TextChoices):
		"""What ordering this item actually does.

		Everything but REGULAR skips the fulfillment queue's usual meaning.
		The two savers are applied the instant they are paid for and are never
		posted to anyone; PRINTER is the end-of-program claim, which does go to
		fulfillment but is not something you can find on a shelf.
		"""
		REGULAR = "regular", "Regular item"
		SAVER_CURRENT = "saver_current", "Streak saver: this week"
		SAVER_PAST = "saver_past", "Streak saver: earliest missed week"
		PRINTER = "printer", "Printer claim (hidden from the shop)"

	name = models.CharField(max_length=60)
	description = models.CharField(max_length=500)
	cost = models.PositiveIntegerField()
	deleted = models.BooleanField(default=False)
	imageUrl = models.URLField(max_length=2048, default="https://example.com")
	category = models.CharField(max_length=40, default="Other")
	stock = models.IntegerField(
		default=-1,
		help_text="Units available to order. -1 means unlimited stock.",
	)
	kind = models.CharField(
		max_length=16,
		choices=Kind.choices,
		default=Kind.REGULAR,
		help_text="Savers are applied on purchase and never reach fulfillment.",
	)
	# "<track slug>:<printer name>" on a PRINTER row, "" on everything else.
	# This is the join back to printers.py, which owns the tree: the rows here
	# exist only so a claim can become an ordinary Order that fulfillment
	# already knows how to handle.
	printer_key = models.CharField(max_length=80, blank=True, default="")

	class Meta:
		constraints = [
			# Unique among the rows that set it, and silent about the rest:
			# every non-printer item leaves it empty, and a UniqueConstraint
			# over "" would let exactly one of them exist.
			models.UniqueConstraint(
				fields=["printer_key"],
				condition=models.Q(printer_key__gt=""),
				name="one_item_per_printer",
			),
		]

	@property
	def unlimited_stock(self):
		return self.stock < 0

	@property
	def in_stock(self):
		return self.unlimited_stock or self.stock > 0

	@property
	def is_saver(self):
		return self.kind in (self.Kind.SAVER_CURRENT, self.Kind.SAVER_PAST)

	@property
	def is_instant(self):
		"""True when ordering this resolves immediately instead of queueing."""
		return self.is_saver

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


class PrinterClaim(models.Model):
	"""The one printer a user cashes their 40 hours in for, once.

	Nothing on a track is bought while the program runs — the map is a plan you
	can change your mind about for eight weeks. This is the single transaction
	at the end that fixes it: it names the printer, records the pearls it cost
	all in (the track's entry pearls plus every upgrade step leading to it) and
	carries the Order that puts it in front of fulfillment.

	OneToOne because you get one printer. An organizer who refunds the order
	deletes the claim, which is what lets the user choose again.
	"""

	user = models.OneToOneField(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="printer_claim",
	)
	track_slug = models.CharField(max_length=32)
	printer_name = models.CharField(max_length=60)
	# What was actually debited, stored rather than recomputed: printers.py is
	# edited between seasons and this has to keep saying what was paid.
	pearls_spent = models.PositiveIntegerField(default=0)
	order = models.ForeignKey(
		Order,
		on_delete=models.SET_NULL,
		related_name="printer_claims",
		null=True,
		blank=True,
	)
	created_at = models.DateTimeField(auto_now_add=True)

	def __str__(self):
		return f"{self.user_id} claimed {self.printer_name} ({self.track_slug})"


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
			# Oversight of the reviewers rather than a tier of review: undoing a
			# T1 decision that shouldn't have been made, and reading the audit
			# trail of review decisions. Makes no decisions of its own.
			("reviewer_lead", "Reviewer Lead: roll back T1 reviews, audit reviews"),
			("fulfillment", "Fulfill shop orders"),
			("organizer", "Access to everything")
		]
	
	def __str__(self):
		return "why are you stringing the permissions class doofus"