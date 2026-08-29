"""Models for the Fallout→Atlantis ported review flow.

These are deliberately *additive*: the existing atlantis_site models
(Journal, LookoutSession, TimelapseReview, ...) are untouched. The Fallout
time-audit queue operates on a Ship and persists its verdicts in the same
shape the Fallout frontend expects (annotations JSON, approved_public_seconds).

Reviews decisions here live entirely in fallout_site — the older
atlantis_site.TimelapseReview flow keeps working (and is what gates the T1
queue); the new Fallout UI is its own thing for now.
"""

import datetime

from django.conf import settings
from django.db import models
from django.utils import timezone


class TimeAuditReview(models.Model):
    """One ship's Fallout-style time audit.

    Mirrors Fallout's `TimeAuditReview`: status + reviewer + the annotation
    tree the review UI edits (`annotations.recordings.<recording_id>` holding
    `description`, `segments` (removed/deflated ranges) and
    `stretch_multiplier`), plus the aggregate the reviewer approves.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        RETURNED = "returned", "Returned"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    ship = models.OneToOneField(
        "atlantis_site.Ship",
        on_delete=models.CASCADE,
        related_name="time_audit_review",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    feedback = models.TextField(blank=True, default="")
    approved_public_seconds = models.IntegerField(null=True, blank=True)
    annotations = models.JSONField(default=dict, blank=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="time_audit_reviews",
    )
    # Claim lifecycle (heartbeat keeps the claim alive while a reviewer has the
    # page open). Same semantics as Fallout: one active claim at a time.
    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Time audit of ship #{self.ship_id} ({self.status})"

    # -- claim helpers (simplified Fallout semantics) --

    def claimed(self):
        return (
            self.claimed_by_id is not None
            and self.claim_expires_at is not None
            and self.claim_expires_at > timezone.now()
        )

    def claimed_by_user(self, user):
        return self.claimed() and self.claimed_by_id == user.id

    def extend_claim(self):
        self.claim_expires_at = timezone.now() + datetime.timedelta(minutes=30)
        self.save(update_fields=["claim_expires_at"])


class ReviewerNote(models.Model):
    """Fallout's project reviewer notes — a per-project scratchpad shared by
    the review queues."""

    project = models.ForeignKey(
        "atlantis_site.Project",
        on_delete=models.CASCADE,
        related_name="reviewer_notes",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviewer_notes",
    )
    body = models.TextField()
    ship = models.ForeignKey(
        "atlantis_site.Ship",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewer_notes",
    )
    review_stage = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Reviewer note on project #{self.project_id} by {self.author_id}"


class ProjectFlag(models.Model):
    project = models.ForeignKey(
        "atlantis_site.Project",
        on_delete=models.CASCADE,
        related_name="review_flags",
    )
    ship = models.ForeignKey(
        "atlantis_site.Ship",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="review_flags",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="project_flags",
    )
    review_stage = models.CharField(max_length=64, blank=True, default="")
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Flag on project #{self.project_id} by {self.reviewer_id}"