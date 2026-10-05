"""Page-load benchmark at production scale. Not a test: run it by hand.

    python manage.py test atlantis_site.tests.perf_bench --keepdb

Seeds ~1800 users' worth of projects, journals, ships, reviews and orders, then
loads every GET page as an organizer and as an ordinary user and prints the
query count and wall time for each. All network access other than the local
database is blocked.
"""
import os
import random
import socket
import time
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ..models import (
	ActiveDay, AuditLog, InternalComment, Item, Journal, Order, Profile, Project,
	Ship, T1, T2, T3, Timelapse, TimelapseReview, User, WeekOutcome,
)
from .base import TEST_STORAGES, TEST_ENCRYPTION_KEY

N_USERS = int(os.environ.get("BENCH_USERS", 1800))
_real_connect = socket.socket.connect


def _local_only(sock, address):
	host = address[0] if isinstance(address, tuple) else address
	if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
		raise OSError(f"network blocked in benchmark: {host}")
	return _real_connect(sock, address)


def seed():
	rng = random.Random(1)
	now = timezone.now()
	week1 = timezone.datetime(2026, 9, 22, 15, tzinfo=timezone.get_current_timezone())
	users = User.objects.bulk_create([
		User(username=f"user{i}", email=f"user{i}@example.com",
			 date_joined=now - timedelta(days=rng.randint(0, 40)))
		for i in range(N_USERS)
	])
	Profile.objects.bulk_create([
		Profile(user=u, verification_status="verified", ysws_eligible=True,
				slack_id=f"U{u.pk:08d}", slack_username=u.username,
				layers=rng.randint(0, 400), last_seen=now - timedelta(hours=rng.randint(0, 200)),
				printer_track=rng.choice(["", "bambu", "creality"]))
		for u in users
	])
	ActiveDay.objects.bulk_create([
		ActiveDay(user=u, day=(now - timedelta(days=d)).date())
		for u in users for d in range(rng.randint(0, 10))
	], ignore_conflicts=True)

	projects = Project.objects.bulk_create([
		Project(owner=u, title=f"Project {u.pk}-{k}", description="desc " * 20,
				printablesUrl="https://www.printables.com/model/1-x",
				editor_model_url="https://cad.onshape.com/documents/abc",
				image_url="images/x.png")
		for u in users for k in range(rng.choice([0, 1, 1, 1, 2, 3]))
	])
	# Ships for ~35% of projects.
	statuses = [Ship.ShipStatus.T1_QUEUE] * 4 + [Ship.ShipStatus.T2_QUEUE] * 2 + [
		Ship.ShipStatus.T3_QUEUE, Ship.ShipStatus.FINALIZED, Ship.ShipStatus.FINALIZED,
		Ship.ShipStatus.REJECTED, Ship.ShipStatus.CHANGES_REQUESTED,
	]
	ship_projects = [p for p in projects if rng.random() < 0.35]
	ships = Ship.objects.bulk_create([
		Ship(project=p, status=rng.choice(statuses)) for p in ship_projects
	])
	ship_by_project = {s.project_id: s for s in ships}

	journals = Journal.objects.bulk_create([
		Journal(project=p, ship=ship_by_project.get(p.pk) if k < 3 else None,
				title=f"Entry {k}", image_url="images/j.png", model_url="models/m.stl")
		for p in projects for k in range(rng.randint(1, 6))
	])
	tl = []
	for n, j in enumerate(journals):
		for m in range(rng.randint(1, 2)):
			tl.append(Timelapse(
				project_id=j.project_id, owner_id=j.project.owner_id,
				journal=j, source="lapse", lapse_id=f"lapse-{n}-{m}", name="tl",
				playback_url="https://cdn.example.com/x.mp4", status="complete",
				tracked_seconds=rng.randint(10, 150) * 60,
				recorded_at=week1 + timedelta(hours=rng.randint(0, 300)),
			))
	Timelapse.objects.bulk_create(tl)

	staff = users[:15]
	org = staff[0]
	org.is_staff = True
	org.is_superuser = False
	org.save()
	perms = Permission.objects.filter(content_type__app_label="atlantis_site")
	for s in staff:
		s.is_staff = True
		s.save()
		s.user_permissions.add(*perms)

	shipped_journals = [j for j in journals if j.ship_id]
	TimelapseReview.objects.bulk_create([
		TimelapseReview(journal=j, reviewer=rng.choice(staff), internal_notes="ok")
		for j in shipped_journals if rng.random() < 0.8
	])
	t1s, t2s, t3s, comments = [], [], [], []
	for s in ships:
		if s.status != Ship.ShipStatus.T1_QUEUE:
			t1s.append(T1(ship=s, reviewer=rng.choice(staff), feedback="fb", internal_notes="n",
						  approved=s.status != Ship.ShipStatus.REJECTED))
			comments.append(InternalComment(ship=s, author=rng.choice(staff), text="hm"))
		if s.status in (Ship.ShipStatus.T3_QUEUE, Ship.ShipStatus.FINALIZED):
			t2s.append(T2(ship=s, reviewer=rng.choice(staff), feedback="fb", justification="j"))
		if s.status == Ship.ShipStatus.FINALIZED:
			t3s.append(T3(ship=s, reviewer=rng.choice(staff), payout_time=300, airtable_time=300,
						  payout_layers=40))
	T1.objects.bulk_create(t1s)
	T2.objects.bulk_create(t2s)
	T3.objects.bulk_create(t3s)
	InternalComment.objects.bulk_create(comments)

	items = Item.objects.bulk_create([
		Item(name=f"Item {i}", description="thing", cost=rng.randint(10, 300),
			 category=rng.choice(["Tools", "Filament", "Stickers"]))
		for i in range(25)
	])
	Order.objects.bulk_create([
		Order(owner=rng.choice(users), item=rng.choice(items), cost=50,
			  status=rng.choice(["P", "P", "F", "D"]))
		for _ in range(1200)
	])
	AuditLog.objects.bulk_create([
		AuditLog(actor=rng.choice(staff), action=rng.choice(["t1_decision", "edit_user", "order"]),
				 target="x", path="/root/", method="POST")
		for _ in range(6000)
	])
	if os.environ.get("BENCH_ANALYZE", "1") == "1":
		with connection.cursor() as cursor:
			cursor.execute("ANALYZE")
	return org


@override_settings(
	STORAGES=TEST_STORAGES, MEDIA_URL="/media/", ADDRESS_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY,
	CHALLENGE_START_DATE="2026-09-21",
)
class PageBenchmark(TestCase):
	@classmethod
	def setUpTestData(cls):
		t = time.perf_counter()
		cls.org = seed()
		print(f"\nseeded in {time.perf_counter() - t:.1f}s")

	def setUp(self):
		p = patch.object(socket.socket, "connect", _local_only)
		p.start()
		self.addCleanup(p.stop)

	def bench(self, label, url, user, runs=2):
		self.client.force_login(user)
		best, n, status = None, 0, None
		for _ in range(runs):
			connection.queries_log.clear()
			with CaptureQueriesContext(connection) as ctx:
				t = time.perf_counter()
				resp = self.client.get(url)
				dt = time.perf_counter() - t
			best = dt if best is None else min(best, dt)
			n, status = len(ctx), resp.status_code
		print(f"{label:<34} {status:>4} {n:>6} q {best * 1000:>8.0f} ms   {url}")
		if os.environ.get("BENCH_SQL") and os.environ["BENCH_SQL"] in url:
			from collections import Counter
			import re
			shapes = Counter(re.sub(r"\d+", "N", q["sql"])[:160] for q in ctx.captured_queries)
			for shape, k in shapes.most_common(8):
				print(f"   x{k:<4} {shape}")
			for q in sorted(ctx.captured_queries, key=lambda q: -float(q["time"]))[:4]:
				print(f"   {float(q['time']) * 1000:7.1f} ms  {q['sql'][:200]}")
			if os.environ.get("BENCH_EXPLAIN"):
				slowest = max(ctx.captured_queries, key=lambda q: float(q["time"]))
				print(slowest["sql"])
				with connection.cursor() as cursor:
					cursor.execute("EXPLAIN ANALYZE " + slowest["sql"])
					print("\n".join(r[0] for r in cursor.fetchall()))
		if os.environ.get("BENCH_PROFILE") and os.environ["BENCH_PROFILE"] in url:
			import cProfile, pstats
			prof = cProfile.Profile()
			prof.enable()
			self.client.get(url)
			prof.disable()
			pstats.Stats(prof).sort_stats(os.environ.get("BENCH_SORT", "cumulative")).print_stats(int(os.environ.get("BENCH_N", 45)))
		return n, best

	def test_pages(self):
		org = self.org
		heavy = Project.objects.values("owner").annotate(n=__import__("django").db.models.Count("journals")).order_by("-n")[0]["owner"]
		user = User.objects.get(pk=heavy)
		project = user.projects.first()
		others = Ship.objects.exclude(project__owner=org).order_by("-id")
		ship_t1 = others.filter(status="T1").exclude(journals__timelapse_review__isnull=True).first()
		ship_t2 = others.filter(status="T2").first()
		ship_t3 = others.filter(status="T3").first()
		tl_project = Journal.objects.filter(timelapse_review__isnull=True, ship__isnull=False).exclude(project__owner=org).first().project_id
		item = Item.objects.filter(kind="regular").first()
		print(f"\n{'page':<34} {'code':>4} {'queries':>8} {'time':>9}")
		pages = [
			("admin home", "/root/"),
			("admin users", "/root/users"),
			("admin projects", "/root/projects/"),
			("admin metrics", "/root/metrics/"),
			("admin challenge", "/root/challenge/"),
			(f"admin challenge user", f"/root/challenge/{user.pk}/"),
			("admin review (T1) dash", "/root/review/"),
			("admin review project", f"/root/review/{ship_t1.pk}"),
			("admin ysws (T2) dash", "/root/ysws_review/"),
			("admin ysws project", f"/root/ysws_review/{ship_t2.pk}"),
			("admin fraud (T3) dash", "/root/fraud_review/"),
			("admin fraud project", f"/root/fraud_review/{ship_t3.pk}"),
			("admin timelapse dash", "/root/timelapse_review/"),
			("admin timelapse project", f"/root/timelapse_review/{tl_project}"),
			("admin review audit", "/root/review/audit/"),
			("admin audit log", "/root/audit_log/"),
			("admin fulfillment", "/root/fulfillment/"),
			("admin shop", "/root/shop/"),
			("admin unfinished csv", "/root/users/unfinished_hours.csv"),
		]
		for label, url in pages:
			self.bench(label, url, org)
		client_pages = [
			("dashboard", "/dashboard/"),
			("projects", "/projects/"),
			("project detail", f"/projects/{project.pk}/"),
			("explore", "/explore/"),
			("shop", "/shop/"),
			("item detail", f"/shop/{item.pk}"),
			("user profile", f"/users/{user.pk}/"),
			("printer select", "/printer-select/"),
			("printer track", "/printer-select/bambu/"),
			("guides", "/guides/"),
		]
		for label, url in client_pages:
			self.bench("[user] " + label, url, user)
		for label, url in client_pages[:4]:
			self.bench("[org] " + label, url, org)
