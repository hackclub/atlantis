from django.db import models
from django.contrib.auth.models import User
from django.conf import settings



class Profile(models.Model):
	user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="hackclub_profile")
	verification_status = models.CharField(max_length=64, blank=True, default="")
	slack_id = models.CharField(max_length=64, blank=True, default="")
	layers = models.IntegerField(default=0)

	def __str__(self):
		return self.user.username

class Project(models.Model):
	owner = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="projects"
	)
	title = models.CharField(max_length=60, default="My Project")
	description = models.CharField(max_length=1000)
	printablesUrl = models.CharField(max_length=150)
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
	class ShipStatus(models.TextChoices):
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
	

class Item(models.Model):
	name = models.CharField(max_length=60)
	description = models.CharField(max_length=100)
	cost = models.PositiveIntegerField()
	deleted = models.BooleanField(default=False)
	imageUrl = models.CharField(max_length=250, default="https://example.com")

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