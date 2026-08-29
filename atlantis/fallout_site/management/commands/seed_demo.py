"""Seed the dev database with a demo reviewer, projects, journals, Lookout
sessions, ships and TimeAuditReviews so the Fallout-style review page has
something to show. Idempotent: re-running recreates the demo users/projects.

Usage: python manage.py seed_demo
"""

from itertools import count

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from atlantis_site.models import Journal, LookoutSession, Profile, Project, Ship
from fallout_site.models import ReviewerNote, TimeAuditReview

_session_seq = count(1)

REVIEWER_USERNAME = "reviewer"
REVIEWER_PASSWORD = "reviewer1234"
OWNER_PASSWORD = "pw"

# Dev-only: sample media is served by the in-process mock Lookout server.
DEV_BASE = "http://localhost:8000"

DEMO_OWNERS = [
    ("ada", "Ada Lovelace", "U0ADA"),
    ("grace", "Grace Hopper", "U0GRACE"),
    ("alan", "Alan Turing", "U0ALAN"),
]

DEMO_PROJECTS = [
    {
        "slug": "parametric-guitar",
        "title": "Parametric Guitar",
        "description": "A fully parametric electric guitar, modeled start to finish in Fusion 360.",
        "image": "guitar",
        "journal_titles": ["Body topology exploration", "Neck + fretboard joint", "Routing cavities", "Assembly + hardware"],
        "seconds": [90 * 60, 75 * 60, 120 * 60, 45 * 60],
    },
    {
        "slug": "robot-arm",
        "title": "Six-Axis Robot Arm",
        "description": "A desktop six-axis robot arm machined from aluminium plate.",
        "image": "robot",
        "journal_titles": ["Joint kinematics sketch", "Base + shoulder mount", "Forearm + wrist"],
        "seconds": [70 * 60, 95 * 60, 60 * 60],
    },
    {
        "slug": "solder-paste-stencil",
        "title": "Solder Paste Stencil Jig",
        "description": "A laser-cut jig for repeatable solder paste application.",
        "image": "stencil",
        "journal_titles": ["Feeler-gauge alignment", "Clamp test with leftover FR4"],
        "seconds": [40 * 60, 55 * 60],
    },
]


def _make_user(username, password, slack_username=None, slack_id="U0X", is_superuser=False, is_staff=False):
    user, created = User.objects.get_or_create(username=username)
    user.set_password(password)
    if slack_username:
        user.first_name = slack_username
    user.is_superuser = is_superuser
    user.is_staff = is_staff
    user.save()
    Profile.objects.get_or_create(
        user=user,
        defaults={
            "verification_status": "verified",
            "ysws_eligible": True,
            "slack_id": slack_id,
            "slack_username": slack_username or username,
            "slack_pfp_url": "https://cdn.hackclub.com/assets/slack_hash_256.png",
        },
    )
    return user


class Command(BaseCommand):
    help = "Seed demo data for the Fallout-style time audit review page."

    def handle(self, *args, **options):
        from django.utils import timezone

        now = timezone.now()

        # --- Reviewer ---
        reviewer = _make_user(
            REVIEWER_USERNAME, REVIEWER_PASSWORD,
            slack_username="⛵ Reviewer", slack_id="U0REVIEWER", is_staff=True, is_superuser=True,
        )
        self.stdout.write(f"reviewer created: {reviewer.username} / {REVIEWER_PASSWORD}")

        # Rebuild demo owners/projects so the command is idempotent. Delete in
        # dependency order: reviews → journals (their ship FK is PROTECT) →
        # the projects themselves (cascades ships, sessions, notes).
        owners = []
        for username, display, slack_id in DEMO_OWNERS:
            owners.append(_make_user(username, OWNER_PASSWORD, slack_username=display, slack_id=slack_id, is_staff=True))

        demo_projects = Project.objects.filter(owner__in=owners)
        TimeAuditReview.objects.filter(project__in=demo_projects).delete()
        Journal.objects.filter(project__in=demo_projects).delete()
        ReviewerNote.objects.filter(project__in=demo_projects).delete()
        demo_projects.delete()

        for project_spec, owner in zip(DEMO_PROJECTS, owners):
            project = Project.objects.create(
                owner=owner,
                title=project_spec["title"],
                description=project_spec["description"],
                editor_model_url=f"https://example.com/{project_spec['slug']}.f3d",
                image_url=f"{DEV_BASE}/mock-lookout/{project_spec['image']}/thumbnail.jpg",
            )

            ship = Ship.objects.create(project=project, status=Ship.ShipStatus.T1_QUEUE)

            for idx, (title, seconds) in enumerate(
                zip(project_spec["journal_titles"], project_spec["seconds"]),
                start=1,
            ):
                journal = Journal.objects.create(
                    project=project,
                    ship=ship,
                    title=title,
                    image_url=f"{DEV_BASE}/mock-lookout/{project_spec['image']}-{idx:02d}/thumbnail.jpg",
                    model_url="https://example.com/model.stl",
                )
                session = LookoutSession.objects.create(
                    project=project,
                    owner=owner,
                    journal=journal,
                    session_id=f"{project_spec['slug']}-{idx:03d}",
                    token=f"demo-token-{next(_session_seq)}",
                    status=LookoutSession.Status.COMPLETE,
                    tracked_seconds=seconds,
                    screenshot_count=seconds // 60,
                )
                self.stdout.write(
                    f"  journal {idx}: {title} ({seconds // 60} min, session {session.session_id})"
                )

            # TimeAuditReview is created automatically by the signal when journals are created
            # Just verify it exists
            review = TimeAuditReview.objects.get(project=project)
            self.stdout.write(f"project: {project.title} (review #{review.id})")

        # One completed review so the "All Time Audits" table has a row.
        archived_owner = owners[-1]
        archived_project, _ = Project.objects.get_or_create(
            owner=archived_owner,
            title="Archived: Time Lapse Pedestal",
            defaults={
                "description": "A finished project that already cleared time audit.",
                "image_url": f"{DEV_BASE}/mock-lookout/pedestal/thumbnail.jpg",
            },
        )
        archived_ship, _ = Ship.objects.get_or_create(
            project=archived_project,
            status=Ship.ShipStatus.FINALIZED,
        )
        archived_journal, _ = Journal.objects.get_or_create(
            project=archived_project,
            ship=archived_ship,
            title="Final lap — pedestal finished",
            defaults={
                "image_url": f"{DEV_BASE}/mock-lookout/pedestal-01/thumbnail.jpg",
                "model_url": "https://example.com/pedestal.stl",
            },
        )
        if not LookoutSession.objects.filter(journal=archived_journal).exists():
            LookoutSession.objects.create(
                project=archived_project,
                owner=archived_owner,
                journal=archived_journal,
                session_id="pedestal-001",
                token=f"demo-token-{next(_session_seq)}",
                status=LookoutSession.Status.COMPLETE,
                tracked_seconds=120 * 60,
                screenshot_count=120,
            )

        # Create an approved time audit review for the archived project
        review, created = TimeAuditReview.objects.get_or_create(
            project=archived_project,
            defaults={
                "status": TimeAuditReview.Status.APPROVED,
                "reviewer": reviewer,
                "reviewed_at": now - timezone.timedelta(days=1),
                "approved_public_seconds": sum(j.tracked_seconds for j in archived_project.journals.all()),
                "feedback": "Clean laps. Nice parametric workflow.",
                "annotations": {"recordings": {}},
                "reviewed_journal_ids": [j.id for j in archived_project.journals.all()],
            },
        )
        if created:
            self.stdout.write("completed time audit review created (approved)")

        self.stdout.write(self.style.SUCCESS("done ✔"))