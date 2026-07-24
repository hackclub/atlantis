import os
from urllib.parse import urlparse

from django.db import models
from django.contrib.auth.models import User
from django.conf import settings
from django.utils import timezone

ALLOWED_EDITORS = [
	"Fusion 360",
	"Onshape",
	"Shapr3D",
	"Solidworks",
	"FreeCAD",
	"OpenSCAD",
	"Blender",
	"Solvespace",
]

EDITOR_FILE_EXTENSIONS = {
	".f3d": "Fusion 360",
	".f3z": "Fusion 360",
	".sldprt": "Solidworks",
	".sldasm": "Solidworks",
	".slddrw": "Solidworks",
	".fcstd": "FreeCAD",
	".scad": "OpenSCAD",
	".blend": "Blender",
	".slvs": "Solvespace",
	".shapr3d": "Shapr3D",
	".shapr": "Shapr3D",
}

EDITOR_LINK_DOMAINS = {
	"onshape.com": "Onshape",
	"a360.co": "Fusion 360",
	"autodesk360.com": "Fusion 360",
	"shapr3d.com": "Shapr3D",
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


# auth model
class Profile(models.Model):
	user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="hackclub_profile")
	verification_status = models.CharField(max_length=64, blank=True, default="")
	slack_id = models.CharField(max_length=64, blank=True, default="")
	slack_username = models.CharField(max_length=64, blank=True, default="")
	slack_pfp_url = models.CharField(max_length=200, blank=True, default="")
	layers = models.IntegerField(default=0)
	print_reward_kg = models.IntegerField(default=0)

	def __str__(self):
		return self.user.username

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
		PRINT_QUEUE = "PQ", "In print queue"
		BEING_PRINTED = "BP", "Being printed"
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
	feedback = models.CharField(max_length=100)
	internal_notes = models.CharField(max_length=100)
	approved = models.BooleanField()
	print = models.BooleanField(default=True)

class Print(models.Model):
	ship = models.ForeignKey(
		Ship,
		on_delete=models.CASCADE,
		related_name="prints"
	)
	printer = models.ForeignKey(
		User,
		on_delete=models.PROTECT,
		related_name="prints"
	)

	class Decision(models.TextChoices):
		RETURN_T1 = "T1", "Returned to T1 Review"
		UNCLAIMED = "U", "Unclaimed, back in printing queue"
		PRINTING = "P", "Being printed"
		APPROVE = "A", "Approve"

	weight = models.IntegerField(null=True, blank=True)
	decision = models.CharField(
		max_length=2,
		choices=Decision.choices,
		default=Decision.PRINTING
	)

	claimed_time = models.DateTimeField(default=timezone.now)
	unclaimed_time = models.DateTimeField(null=True, blank=True)
	finished_time = models.DateTimeField(null=True, blank=True)

	image_url = models.CharField(max_length=2048, blank=True)

	feedback = models.CharField(max_length=100, blank=True)
	internal_notes = models.CharField(max_length=100, blank=True)

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
		RETURN_PRINT = "P", "Returned to Printers"
		APPROVE = "A", "Approved"

	reviewed_at = models.DateTimeField(auto_now_add=True)
	decision = models.CharField(
		max_length=2,
		choices=Decision.choices,
		default=Decision.APPROVE
	)

	deductions = models.IntegerField(default=0)
	feedback = models.CharField(max_length=100)
	justification = models.CharField(max_length=400)

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
		RETURN_PRINT = "P", "Returned to Printers"
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

	internal_notes = models.CharField(blank=True)

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

	time_spent = models.IntegerField()
	created_at = models.DateTimeField(auto_now_add=True)
	title = models.CharField(max_length=100)
	text = models.CharField(max_length=2000)
	image_url = models.CharField(max_length=2048)
	model_url = models.CharField(max_length=2048)

# lookout timelapse recording sessions
class LookoutSession(models.Model):
	# Mirrors Lookout's server-side session lifecycle. See the Lookout guide.
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

	# Lookout identifiers. `token` is a secret credential granting full control
	# of the session — never render it anywhere except the recorder page JS for
	# its own session, and never log it in full.
	session_id = models.CharField(max_length=64, unique=True)
	token = models.CharField(max_length=128, unique=True)

	status = models.CharField(
		max_length=16,
		choices=Status.choices,
		default=Status.PENDING,
	)

	# Cached server-authoritative values, refreshed via the internal API.
	tracked_seconds = models.IntegerField(default=0)
	total_active_seconds = models.IntegerField(default=0)
	screenshot_count = models.IntegerField(default=0)

	# Whether Hackatime heartbeats have already been forwarded for this session,
	# so we never double-count a completed timelapse.
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
	def tracked_display(self):
		total = self.tracked_seconds or 0
		return f"{total // 3600}h {(total % 3600) // 60}m"

	@property
	def video_url(self):
		# Permanent media redirect — safe to embed, unlike expiring presigned URLs.
		base = settings.LOOKOUT_BASE_URL.rstrip("/")
		return f"{base}/api/media/{self.session_id}/video.mp4"

	@property
	def thumbnail_url(self):
		base = settings.LOOKOUT_BASE_URL.rstrip("/")
		return f"{base}/api/media/{self.session_id}/thumbnail.jpg"


# shop models
class Item(models.Model):
	name = models.CharField(max_length=60)
	description = models.CharField(max_length=100)
	cost = models.PositiveIntegerField()
	deleted = models.BooleanField(default=False)
	imageUrl = models.URLField(max_length=2048, default="https://example.com")
	category = models.CharField(max_length=40, default="Other")
	is_print_reward = models.BooleanField(default=False)
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
			("printer", "Project Printer"),
			("fulfillment", "Fulfill shop orders"),
			("organizer", "Access to everything")
		]
	
	def __str__(self):
		return "why are you stringing the permissions class doofus"