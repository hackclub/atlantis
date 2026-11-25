import uuid
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from .. import lddb
from ..models import (
	LddbCursor, Timelapse, TimelapseAnnotation, TimelapseRemoval, TimelapseReview, TimelapseReviewEdit,
)
from .base import (
	BaseTestCase, approve_timelapse, during_week, grant_perms, make_journal, make_lookout,
	make_project, make_user,
)

KINDS = [
	{"id": "idle", "label": "Idle"},
	{"id": "invalid", "label": "Not this project"},
	{"id": "plan", "label": "Planning"},
	{"id": "ai-help", "label": "Waiting on AI"},
	{"id": "other", "label": "Other"},
]


class FakeLddb:
	def __init__(self):
		self.deflations = {}
		self.events = []
		self.calls = []
		self.down = False
		self.race = {}

	def __call__(self, method, path, *, json=None, params=None):
		self.calls.append((method, path, json))
		if self.down:
			raise lddb.LddbError("LDDB GET failed: connection refused")
		if path == "/deflations/kinds":
			return KINDS
		if path == "/deflations/changes":
			after = params["after"]
			items = [e["item"] for e in self.events if e["item"]["id"] > after][: params["limit"]]
			return {"items": items, "next": items[-1]["id"] if items else after}
		parts = path.strip("/").split("/")
		external_id = parts[1]
		if len(parts) == 3 and parts[2] == "history":
			entries = [e["history"] for e in self.events if e["item"]["external_id"] == external_id]
			return list(reversed(entries)) or None
		if method == "GET":
			return self.deflations.get(external_id)
		if external_id in self.race:
			self.write(external_id, self.race.pop(external_id), actor="user:someone", program="fallout")
		current = self.deflations.get(external_id)
		if (current["version"] if current else None) != json.get("version"):
			raise lddb.LddbStale(current)
		if method == "PUT":
			marks = [self._mark(m) for m in json["marks"]]
			idle = json.get("idle")
		else:
			marks = [m for m in current["marks"] if m["id"] not in json.get("remove", [])]
			marks += [self._mark(m) for m in json.get("add", [])]
			idle = current["idle"]
		return self.write(
			external_id, marks, description=json.get("description", ""), idle=idle,
			actor="key:atlantis", program=json.get("program"), note=json.get("note"),
		)

	@staticmethod
	def _mark(m):
		return {"id": m.get("id") or str(uuid.uuid4()), "text": "", **m, "fraction": float(m.get("fraction", 1))}

	def write(self, external_id, marks, description=None, idle=None, actor="user:someone",
			program="fallout", note=None, by=None):
		current = self.deflations.get(external_id)
		version = (current["version"] if current else 0) + 1
		view = {
			"external_id": external_id,
			"version": version,
			"marks": sorted(marks, key=lambda m: m["start_ms"]),
			"description": description if description is not None else (current or {}).get("description", ""),
			"idle": idle or (current or {}).get("idle") or {"applied": False, "ranges": []},
		}
		self.deflations[external_id] = view
		self._event("updated" if current else "created", view, actor, program, note, by)
		return view

	def clear(self, external_id, by=None):
		version = self.deflations.pop(external_id)["version"] + 1
		self._event("cleared", {"external_id": external_id, "version": version}, "user:someone", "lddb", "wrong recording", by)

	def _event(self, kind, view, actor, program, note, by):
		event_id = len(self.events) + 1
		self.events.append({
			"item": {
				"id": event_id, "kind": kind, "external_id": view["external_id"],
				"version": view["version"], "actor": actor, "program": program,
			},
			"history": {
				"id": event_id, "kind": kind, "version": view["version"], "actor": actor,
				"program": program, "note": note, "by": by or {"name": "Sam", "slack_id": "U0SAM"},
			},
		})

	def writes(self):
		return [(m, p, b) for m, p, b in self.calls if m in ("PUT", "PATCH")]


@override_settings(LDDB_API_KEY="lddb_test", LDDB_API_BASE_URL="https://lddb.test/api")
class LddbTestCase(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.fake = FakeLddb()
		patcher = patch("atlantis_site.lddb._request", side_effect=self.fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.owner = make_user("owner", slack_id="U0OWNER")
		self.project = make_project(self.owner, title="Boat")
		self.reviewer = grant_perms(make_user("tlrev", slack_id="U0TL"), "timelapse_review")

	def sign_off(self, journal, ranges=(), description="watched it"):
		self.client.force_login(self.reviewer)
		session = journal.timelapses.get()
		return self.client.post(reverse("timelapse_decision", args=[self.project.id]), {
			"internal_notes": "",
			"removal_session": [str(session.id)] * len(ranges),
			"removal_start": [r[0] for r in ranges],
			"removal_end": [r[1] for r in ranges],
			"removal_reason": [r[2] for r in ranges],
			f"description_{session.id}": description,
		})

	def signed_off(self, minutes=180, ranges=()):
		journal = make_journal(self.project, time_spent=minutes)
		self.sign_off(journal, ranges)
		return journal.timelapses.get()


class PushTests(LddbTestCase):
	def test_a_sign_off_writes_the_recordings_deflation(self):
		journal = make_journal(self.project, time_spent=180)
		session = journal.timelapses.get()
		session.inactive_segments = [{"start": 5, "end": 9, "duration": 4}]
		session.save()
		self.sign_off(journal, [("0:10", "0:40", "AFK / idle screen"), ("1:00", "1:30", "watching youtube")])

		method, path, body = self.fake.writes()[0]
		self.assertEqual((method, path), ("PUT", f"/deflations/{session.lapse_id}"))
		self.assertEqual(body["program"], "atlantis")
		self.assertEqual(body["description"], "watched it")
		self.assertIn("tlrev", body["note"])
		self.assertEqual(body["idle"], {"applied": False, "ranges": [[5000, 9000]]})
		self.assertEqual(
			[(m["start_ms"], m["end_ms"], m["reason"], m["fraction"], m["text"]) for m in body["marks"]],
			[(10000, 40000, "idle", 1, "AFK / idle screen"), (60000, 90000, "other", 1, "watching youtube")],
		)

		session.refresh_from_db()
		self.assertFalse(session.lddb_pending)
		self.assertEqual(session.lddb_version, 1)
		view = self.fake.deflations[session.lapse_id]
		self.assertEqual(
			sorted(str(r.lddb_mark_id) for r in session.removals.all()),
			sorted(m["id"] for m in view["marks"]),
		)
		self.assertEqual(sorted(session.lddb_marks), sorted(m["id"] for m in view["marks"]))
		self.assertFalse(TimelapseReviewEdit.objects.exists())

	def test_a_sign_off_with_nothing_cut_still_says_so(self):
		session = self.signed_off()
		self.assertEqual(self.fake.deflations[session.lapse_id]["marks"], [])

	def test_the_reviewer_sees_nothing_when_lddb_is_down(self):
		self.fake.down = True
		journal = make_journal(self.project, time_spent=60)
		response = self.sign_off(journal, [("0:10", "0:20", "AFK / idle screen")])
		self.assertEqual(response.status_code, 302)
		self.assertTrue(TimelapseReview.objects.filter(journal=journal).exists())
		session = journal.timelapses.get()
		session.refresh_from_db()
		self.assertTrue(session.lddb_pending)
		self.assertIn("connection refused", session.lddb_error)

		self.fake.down = False
		call_command("sync_lddb", stdout=StringIO())
		session.refresh_from_db()
		self.assertFalse(session.lddb_pending)
		self.assertEqual(session.lddb_error, "")
		self.assertEqual(len(self.fake.deflations[session.lapse_id]["marks"]), 1)

	def test_a_recording_lddb_refuses_stops_being_retried(self):
		journal = make_journal(self.project, time_spent=60)
		session = journal.timelapses.get()
		real = self.fake.__call__

		def refuse(method, path, **kwargs):
			if method == "PUT":
				raise lddb.LddbRejected(f"LDDB PUT {path} returned 400: legacy recording")
			return real(method, path, **kwargs)

		with patch("atlantis_site.lddb._request", side_effect=refuse):
			self.sign_off(journal)
		session.refresh_from_db()
		self.assertFalse(session.lddb_pending)
		self.assertIn("legacy recording", session.lddb_error)

	@override_settings(LDDB_API_KEY="")
	def test_without_a_key_nothing_is_sent_but_the_review_is_queued(self):
		session = self.signed_off(ranges=[("0:10", "0:20", "Other")])
		self.assertEqual(self.fake.calls, [])
		session.refresh_from_db()
		self.assertTrue(session.lddb_pending)

	def test_legacy_lookout_footage_is_left_out(self):
		journal = make_journal(self.project, time_spent=0)
		make_lookout(self.project, journal=journal, minutes=60)
		self.sign_off(journal)
		self.assertEqual(self.fake.writes(), [])
		self.assertFalse(Timelapse.objects.filter(lddb_pending=True).exists())

	def test_the_first_push_replaces_what_another_program_wrote(self):
		journal = make_journal(self.project, time_spent=60)
		session = journal.timelapses.get()
		self.fake.write(session.lapse_id, [{"id": str(uuid.uuid4()), "start_ms": 0, "end_ms": 5000, "reason": "plan", "fraction": 0.5, "text": ""}])
		self.sign_off(journal, [("0:10", "0:20", "Other")])
		_, _, body = self.fake.writes()[-1]
		self.assertEqual(body["version"], 1)
		self.assertEqual([m["start_ms"] for m in self.fake.deflations[session.lapse_id]["marks"]], [10000])

	def test_a_write_that_lands_mid_push_is_read_and_retried(self):
		session = self.signed_off(ranges=[("0:10", "0:20", "Other")])
		review = TimelapseReview.objects.get(journal=session.journal)
		lead = grant_perms(make_user("lead", slack_id="U0LEAD"), "reviewer_lead")
		self.client.force_login(lead)
		existing = list(self.fake.deflations[session.lapse_id]["marks"])
		self.fake.race[session.lapse_id] = existing + [
			{"id": str(uuid.uuid4()), "start_ms": 100000, "end_ms": 110000, "reason": "ai-help", "fraction": 0.0, "text": ""},
		]
		self.client.post(reverse("lapse_review_save", args=[review.id]), {
			"edit_note": "missed one",
			"removal_session": [str(session.id)],
			"removal_start": ["0:30"],
			"removal_end": ["0:40"],
			"removal_reason": ["AFK / idle screen"],
		})
		session.refresh_from_db()
		self.assertFalse(session.lddb_pending)
		marks = self.fake.deflations[session.lapse_id]["marks"]
		self.assertEqual([m["start_ms"] for m in marks], [10000, 30000, 100000])


	def test_a_push_whose_links_were_lost_isnt_sent_twice(self):
		session = self.signed_off(ranges=[("0:10", "0:20", "Other")])
		ids = list(session.removals.values_list("lddb_mark_id", flat=True))
		TimelapseRemoval.objects.filter(session=session).update(lddb_mark_id=None)
		Timelapse.objects.filter(pk=session.pk).update(lddb_pending=True)
		self.assertTrue(lddb.push(session.id))
		_, _, body = self.fake.writes()[-1]
		self.assertEqual((body["add"], body["remove"]), ([], []))
		self.assertEqual(list(session.removals.values_list("lddb_mark_id", flat=True)), ids)
		self.assertEqual(len(self.fake.deflations[session.lapse_id]["marks"]), 1)


class CorrectionPushTests(LddbTestCase):
	def setUp(self):
		super().setUp()
		self.session = self.signed_off(ranges=[("0:10", "0:20", "Other"), ("0:30", "0:40", "AFK / idle screen")])
		self.review = TimelapseReview.objects.get(journal=self.session.journal)
		self.lead = grant_perms(make_user("lead", slack_id="U0LEAD"), "reviewer_lead")
		self.client.force_login(self.lead)
		marks = self.fake.deflations[self.session.lapse_id]["marks"]
		self.note = {"id": str(uuid.uuid4()), "start_ms": 50000, "end_ms": 60000, "reason": "ai-help", "fraction": 0.0, "text": ""}
		self.fake.write(self.session.lapse_id, marks + [self.note], description="watched it")
		lddb.pull()

	def test_a_correction_sends_only_what_it_changed(self):
		first = self.review.removals.get(start_seconds=600)
		self.client.post(reverse("lapse_review_save", args=[self.review.id]), {
			"edit_note": "that was work",
			"delete_removal": [str(first.id)],
			"removal_session": [str(self.session.id)],
			"removal_start": ["1:10"],
			"removal_end": ["1:20"],
			"removal_reason": ["Non-project activity"],
		})
		method, _, body = self.fake.writes()[-1]
		self.assertEqual(method, "PATCH")
		self.assertEqual(body["remove"], [str(first.lddb_mark_id)])
		self.assertEqual([(m["start_ms"], m["reason"]) for m in body["add"]], [(70000, "invalid")])
		self.assertIn("lead", body["note"])
		self.assertIn("that was work", body["note"])
		marks = self.fake.deflations[self.session.lapse_id]["marks"]
		self.assertEqual([m["start_ms"] for m in marks], [30000, 50000, 70000])
		self.session.refresh_from_db()
		self.assertEqual(self.session.lddb_version, self.fake.deflations[self.session.lapse_id]["version"])

	def test_a_cut_over_a_mark_atlantis_doesnt_mirror_takes_its_place(self):
		self.client.post(reverse("lapse_review_save", args=[self.review.id]), {
			"edit_note": "that was idle too",
			"removal_session": [str(self.session.id)],
			"removal_start": ["0:50"],
			"removal_end": ["0:55"],
			"removal_reason": ["AFK / idle screen"],
		})
		_, _, body = self.fake.writes()[-1]
		self.assertEqual(body["remove"], [self.note["id"]])
		self.session.refresh_from_db()
		self.assertFalse(self.session.lddb_pending)


@during_week(2)
class PullTests(LddbTestCase):
	def setUp(self):
		super().setUp()
		self.session = self.signed_off(minutes=180, ranges=[("0:10", "0:20", "Other")])
		self.review = TimelapseReview.objects.get(journal=self.session.journal)
		self.ours = self.fake.deflations[self.session.lapse_id]["marks"][0]
		self.dm = self.slack_dm_mocks["timelapse_review"]
		self.dm.reset_mock()

	def mark(self, start, end, reason="invalid", fraction=1.0, text=""):
		return {"id": str(uuid.uuid4()), "start_ms": start * 1000, "end_ms": end * 1000,
				"reason": reason, "fraction": fraction, "text": text}

	def test_marks_added_elsewhere_become_removals(self):
		reviewer = make_user("sam", slack_id="U0SAM")
		self.fake.write(self.session.lapse_id, [
			self.ours,
			self.mark(30, 90, text="browsing reddit"),
			self.mark(100, 120, reason="plan", fraction=0.5),
			self.mark(130, 140, reason="ai-help", fraction=0.0),
		], note="missed these", by={"name": "Sam", "slack_id": "U0SAM"})

		self.assertEqual(lddb.pull(), 1)
		removals = list(self.review.removals.order_by("start_seconds"))
		self.assertEqual(
			[(r.start_seconds, r.end_seconds) for r in removals],
			[(600, 1200), (1800, 5400), (6000, 6600)],
		)
		self.assertEqual(removals[1].reason, "browsing reddit")
		self.assertEqual(removals[2].reason, "Planning (50% of 1:40-2:00 taken off in LDDB)")
		self.assertTrue(all(r.counts_toward_makeup for r in removals[1:]))

		edit = TimelapseReviewEdit.objects.get(review=self.review)
		self.assertEqual(edit.editor, reviewer)
		self.assertEqual(edit.note, "Changed in LDDB by Sam in fallout: missed these")
		self.assertEqual(len(edit.changes["removals_added"]), 2)
		self.assertEqual(edit.changes["removed_seconds_after"], 600 + 3600 + 600)
		self.dm.assert_called_once()

	def test_a_pulled_mark_goes_back_out_whole(self):
		half = self.mark(100, 120, reason="plan", fraction=0.5)
		self.fake.write(self.session.lapse_id, [self.ours, half])
		lddb.pull()
		self.session.refresh_from_db()
		version = self.session.lddb_version
		lead = grant_perms(make_user("lead", slack_id="U0LEAD"), "reviewer_lead")
		self.client.force_login(lead)
		self.client.post(reverse("lapse_review_save", args=[self.review.id]), {
			"edit_note": "more", "removal_session": [str(self.session.id)],
			"removal_start": ["2:30"], "removal_end": ["2:40"], "removal_reason": ["Other"],
		})
		_, _, body = self.fake.writes()[-1]
		self.assertEqual(body["version"], version)
		self.assertEqual(body["remove"], [])
		marks = {m["id"]: m for m in self.fake.deflations[self.session.lapse_id]["marks"]}
		self.assertEqual(marks[half["id"]], half)

	def test_a_pulled_mark_survives_a_whole_rewrite(self):
		half = self.mark(100, 120, reason="plan", fraction=0.5)
		self.fake.write(self.session.lapse_id, [self.ours, half])
		lddb.pull()
		Timelapse.objects.filter(pk=self.session.pk).update(lddb_pending=True)
		self.fake.clear(self.session.lapse_id)
		self.assertTrue(lddb.push(self.session.id))
		method, _, body = self.fake.writes()[-1]
		self.assertEqual(method, "PUT")
		self.assertIn(half, body["marks"])

	def test_a_mark_removed_elsewhere_removes_the_cut(self):
		self.fake.write(self.session.lapse_id, [], note="that was work")
		lddb.pull()
		self.assertFalse(self.review.removals.exists())
		edit = TimelapseReviewEdit.objects.get(review=self.review)
		self.assertEqual(edit.changes["removals_deleted"][0]["start_seconds"], 600)

	def test_a_changed_mark_replaces_the_cut(self):
		self.fake.write(self.session.lapse_id, [{**self.ours, "end_ms": 30000}])
		lddb.pull()
		removal = self.review.removals.get()
		self.assertEqual((removal.start_seconds, removal.end_seconds), (600, 1800))
		self.assertEqual(str(removal.lddb_mark_id), self.ours["id"])

	def test_a_cleared_deflation_counts_in_full_again(self):
		self.fake.clear(self.session.lapse_id)
		lddb.pull()
		self.assertFalse(self.review.removals.exists())
		self.session.refresh_from_db()
		self.assertEqual(self.session.lddb_version, 2)

	def test_a_new_description_rewrites_the_recordings(self):
		self.fake.write(self.session.lapse_id, [self.ours], description="CAD the whole time")
		lddb.pull()
		annotation = TimelapseAnnotation.objects.get(review=self.review, session=self.session)
		self.assertEqual(annotation.description, "CAD the whole time")

	def test_our_own_writes_are_not_applied_back(self):
		lddb.pull()
		self.assertFalse(TimelapseReviewEdit.objects.exists())
		self.assertEqual(LddbCursor.objects.get().position, len(self.fake.events))

	def test_an_unknown_editor_is_filed_under_the_sync_account(self):
		self.fake.write(self.session.lapse_id, [], by={"name": "Stranger", "slack_id": "U0NOBODY"})
		lddb.pull()
		edit = TimelapseReviewEdit.objects.get(review=self.review)
		self.assertEqual(edit.editor.username, lddb.SYNC_USERNAME)
		self.assertFalse(edit.editor.is_active)

	def test_applying_twice_changes_nothing(self):
		self.fake.write(self.session.lapse_id, [self.ours, self.mark(30, 90)])
		lddb.pull()
		LddbCursor.objects.update(position=0)
		lddb.pull()
		self.assertEqual(TimelapseReviewEdit.objects.count(), 1)
		self.assertEqual(self.review.removals.count(), 2)

	def test_a_recording_not_signed_off_here_is_left_alone(self):
		journal = make_journal(self.project, time_spent=60)
		other = journal.timelapses.get()
		self.fake.write(other.lapse_id, [self.mark(0, 10)])
		lddb.pull()
		self.assertFalse(TimelapseRemoval.objects.filter(session=other).exists())

	def test_a_review_waiting_to_push_isnt_overwritten_by_a_pull(self):
		Timelapse.objects.filter(pk=self.session.pk).update(lddb_pending=True)
		self.fake.write(self.session.lapse_id, [])
		lddb.pull()
		self.assertTrue(self.review.removals.exists())


class BackfillTests(LddbTestCase):
	def test_reviews_from_before_the_sync_are_pushed_on_backfill(self):
		journal = make_journal(self.project, time_spent=60)
		session = journal.timelapses.get()
		approve_timelapse(journal, removals=[(session, 600, 1200, "afk")])
		call_command("sync_lddb", stdout=StringIO())
		self.assertNotIn(session.lapse_id, self.fake.deflations)

		call_command("sync_lddb", "--backfill", stdout=StringIO())
		marks = self.fake.deflations[session.lapse_id]["marks"]
		self.assertEqual([(m["start_ms"], m["end_ms"]) for m in marks], [(10000, 20000)])
