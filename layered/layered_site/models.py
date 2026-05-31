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
	printablesUrl = models.CharField()
	created_at = models.DateTimeField(auto_now_add=True)

	def __str__(self):
		return f"{self.id}: {self.title}"