from django.db import models
from django.contrib.auth.models import User
from django.conf import settings
from django.utils import timezone


# auth model
class Profile(models.Model):
	user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="hackclub_profile")
	verification_status = models.CharField(max_length=64, blank=True, default="")
	slack_id = models.CharField(max_length=64, blank=True, default="")
	slack_username = models.CharField(max_length=64, blank=True, default="")
	slack_pfp_url = models.CharField(max_length=200, blank=True, default="")
	layers = models.IntegerField(default=0)

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
	created_at = models.DateTimeField(auto_now_add=True)
	locked = models.BooleanField(default=False)
	deleted = models.BooleanField(default=False)

	def __str__(self):
		return f"{self.id}: {self.title}"
	
class Ship(models.Model):
	project = models.ForeignKey(
		Project,
		on_delete=models.CASCADE,
		related_name="ships"
	)
	created_at = models.DateTimeField(auto_now_add=True)
	submitted_time = models.IntegerField(default=0)
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
		APPROVE = "A", "Approve"

	weight = models.IntegerField(null=True, blank=True)
	decision = models.CharField(
		max_length=2,
		choices=Decision.choices,
		default=Decision.APPROVE
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

	payout_hours = models.DecimalField(decimal_places=2, max_digits=5)
	airtable_hours = models.DecimalField(decimal_places=2, max_digits=5)

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

# shop models
class Item(models.Model):
	name = models.CharField(max_length=60)
	description = models.CharField(max_length=100)
	cost = models.PositiveIntegerField()
	deleted = models.BooleanField(default=False)
	imageUrl = models.CharField(max_length=2048, default="https://example.com")

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