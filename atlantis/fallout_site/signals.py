"""Django signals for fallout_site models."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from atlantis_site.models import Journal
from fallout_site.models import TimeAuditReview


@receiver(post_save, sender=Journal)
def create_or_update_time_audit_review(sender, instance, created, **kwargs):
    """Create or update TimeAuditReview when a journal is created/updated.

    A project gets a TimeAuditReview as soon as it has at least one journal.
    The review tracks which journals have been reviewed via reviewed_journal_ids.
    """
    project = instance.project

    # Get or create the time audit review for this project
    review, _ = TimeAuditReview.objects.get_or_create(
        project=project,
        defaults={
            "status": TimeAuditReview.Status.PENDING,
            "annotations": {"recordings": {}},
            "reviewed_journal_ids": [],
        },
    )

    # If the review was completed (approved/returned/rejected) but a new journal
    # was added, reopen it so the new content gets reviewed
    if created and review.status in (
        TimeAuditReview.Status.APPROVED,
        TimeAuditReview.Status.RETURNED,
        TimeAuditReview.Status.REJECTED,
    ):
        # Keep existing reviewed_journal_ids but don't include the new journal
        review.status = TimeAuditReview.Status.PENDING
        review.save(update_fields=["status", "updated_at"])