from django.db import models
from django.contrib.auth.models import User
from django.conf import settings



class Profile(models.Model):
	user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="hackclub_profile")
	verification_status = models.CharField(max_length=64, blank=True, default="")
	slack_id = models.CharField(max_length=64, blank=True, default="")

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

	def __str__(self):
		return f"{self.id}: {self.title}"
	
class Ship(models.Model):
	project = models.ForeignKey(
		Project,
		on_delete=models.CASCADE,
		related_name="ships"
	)
	created_at = models.DateTimeField(auto_now_add=True)
	class ProjectStatus(models.TextChoices):
			T1_QUEUE = "T1", "Under T1 Review"
			PRINT_QUEUE = "PQ", "In print queue"
			BEING_PRINTED = "BP", "Being printed"
			T2_QUEUE = "T2", "Under T2 Review"
			T3_QUEUE = "T3", "Under fraud review"
			FINALIZED = "F", "Finalized"
		
	status = models.CharField(
		max_length=2,
		choices=ProjectStatus.choices,
		default=ProjectStatus.T1_QUEUE,
	)

	def __str__(self):
		return f"Ship created at {self.created_at} with status {self.status}"
	

class Item(models.Model):
	name = models.CharField(max_length=60)
	description = models.CharField(max_length=100)
	cost = models.PositiveIntegerField()

	def __str__(self):
		return f"{self.name} ({self.description}) for {self.cost} layers"

class Permissions(models.Model):
	verbose_name = "Permission"
	verbose_name_plural = "Permissions"
	class Meta:
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