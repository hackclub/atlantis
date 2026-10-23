"""Deflation makeup, corrections to signed-off lapse reviews, and saver loans."""

from django.core.cache import cache
from django.urls import reverse

from .. import challenge
from ..models import (
	AuditLog, Item, Order, Profile, SaverCredit, TimelapseAnnotation, TimelapseRemoval,
	TimelapseReview, TimelapseReviewEdit,
)
from .base import (
	BaseTestCase,
	approve_timelapse,
	during_week,
	grant_perms,
	make_journal,
	make_project,
	make_timelapse,
	make_user,
	message_texts,
)


def _pass(project, ranges=()):
	"""POST data for a timelapse review pass. `ranges` is (session, start, end, reason)."""
	data = {
		"internal_notes": "reviewed",
		"removal_session": [str(r[0].id) for r in ranges],
		"removal_start": [r[1] for r in ranges],
		"removal_end": [r[2] for r in ranges],
		"removal_reason": [r[3] for r in ranges],
	}
	for journal in project.journals.filter(timelapse_review__isnull=True):
		for session in journal.timelapses.all():
			data[f"description_{session.id}"] = "watched it"
	return data


class MakeupTests(BaseTestCase):
	"""Deflated time is owed on top of the week's five, once it passes an hour."""

	def setUp(self):
		super().setUp()
		self.owner = make_user("owner", slack_id="U0OWNER")
		self.project = make_project(self.owner)
		self.reviewer = grant_perms(make_user("tlrev", slack_id="U0TL"), "timelapse_review")
		self.client.force_login(self.reviewer)
		self.dm = self.slack_dm_mocks["timelapse_review"]

	def _review(self, minutes, cut):
		"""Journal `minutes` of time now and have the reviewer cut 0:00-`cut` from it."""
		journal = make_journal(self.project, time_spent=minutes)
		session = journal.timelapses.get()
		self.client.post(
			reverse("timelapse_decision", args=[self.project.id]),
			_pass(self.project, [(session, "0:00", cut, "afk")]),
		)
		return journal

	def test_a_cut_over_an_hour_is_owed_this_week(self):
		with during_week(3):
			self._review(180, "1:30")
			week = challenge.standing(self.owner).current
			self.assertEqual(week.makeup_minutes, 90)
			self.assertEqual(week.required_minutes, 300 + 90)
			# 180 tracked minutes still count, so 210 are left, not 120.
			self.assertEqual(week.left_minutes, 210)
			self.assertTrue(TimelapseRemoval.objects.get().counts_toward_makeup)

	def test_the_owner_is_told_how_much_to_make_up(self):
		with during_week(3):
			self._review(180, "1:30")
		self.dm.assert_called_once()
		text, slack_id = self.dm.call_args.args
		self.assertEqual(slack_id, "U0OWNER")
		self.assertIn("1h 30m need to be made up because of deflations to your project", text)

	def test_an_hour_or_less_is_forgiven_and_nobody_is_told(self):
		with during_week(3):
			self._review(180, "1:00")
			self.assertEqual(challenge.standing(self.owner).current.makeup_minutes, 0)
		self.dm.assert_not_called()

	def test_cuts_add_up_within_the_week(self):
		with during_week(3):
			self._review(60, "0:40")
			self.dm.assert_not_called()
			self._review(60, "0:40")
			self.assertEqual(challenge.standing(self.owner).current.makeup_minutes, 80)
		self.dm.assert_called_once()

	def test_cuts_from_before_makeup_existed_are_not_charged(self):
		with during_week(3):
			journal = make_journal(self.project, time_spent=180)
			session = journal.timelapses.get()
			# What a review written before this feature looks like.
			approve_timelapse(journal, removals=[(session, 0, 120 * 60)])
			self.assertEqual(challenge.standing(self.owner).current.makeup_minutes, 0)

	def test_a_week_short_only_by_makeup_is_not_met(self):
		with during_week(3):
			journal = make_journal(self.project, time_spent=360)
			session = journal.timelapses.get()
			self.client.post(
				reverse("timelapse_decision", args=[self.project.id]),
				_pass(self.project, [(session, "0:00", "2:00", "afk")]),
			)
			week = challenge.standing(self.owner).current
			self.assertEqual(week.credited_minutes, 360)
			self.assertFalse(week.met)
			self.assertEqual(week.shortfall_minutes, 60)

	def test_the_whole_field_agrees_with_one_users_standing(self):
		with during_week(3):
			self._review(180, "1:30")
			single = challenge.standing(self.owner).current
			field = challenge.standings()[self.owner.id].current
			self.assertEqual(field.makeup_minutes, single.makeup_minutes)
			self.assertEqual(field.required_minutes, single.required_minutes)

	def test_the_deck_shows_the_makeup(self):
		with during_week(3):
			self._review(180, "1:30")
			self.client.force_login(self.owner)
			response = self.client.get(reverse("dashboard"))
		self.assertContains(response, "1h 30m need to be made up because of deflations")
		self.assertContains(response, "6h 30m")

	def test_a_deleted_project_owes_nothing(self):
		with during_week(3):
			self._review(180, "1:30")
			self.project.deleted = True
			self.project.save()
			self.assertEqual(challenge.standing(self.owner).current.makeup_minutes, 0)


class LapseReviewEditTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.owner = make_user("owner", slack_id="U0OWNER")
		self.project = make_project(self.owner, title="Boat")
		self.journal = make_journal(self.project, time_spent=180)
		self.session = self.journal.timelapses.get()
		self.review = approve_timelapse(
			self.journal, removals=[(self.session, 0, 30 * 60, "idle")]
		)
		self.original = self.review.removals.get()
		TimelapseAnnotation.objects.create(
			review=self.review, session=self.session, description="old words"
		)
		self.lead = grant_perms(make_user("lead", slack_id="U0LEAD"), "reviewer_lead")
		self.client.force_login(self.lead)

	def _save(self, **data):
		data.setdefault("edit_note", "fixing a mistake")
		return self.client.post(reverse("lapse_review_save", args=[self.review.id]), data, follow=True)

	def _add(self, start, end, reason="afk"):
		return {
			"removal_session": [str(self.session.id)],
			"removal_start": [start],
			"removal_end": [end],
			"removal_reason": [reason],
		}

	def test_only_leads_and_organizers_may_edit(self):
		url = reverse("lapse_review_edit", args=[self.review.id])
		reviewer = grant_perms(make_user("tlrev", slack_id="U0TL"), "timelapse_review")
		self.client.force_login(reviewer)
		self.assertEqual(self.client.get(url).status_code, 302)
		self.client.force_login(self.lead)
		self.assertEqual(self.client.get(url).status_code, 200)
		organizer = grant_perms(make_user("org", slack_id="U0ORG"), "organizer")
		self.client.force_login(organizer)
		self.assertEqual(self.client.get(url).status_code, 200)

	def test_a_lead_cannot_edit_a_review_of_their_own_project(self):
		grant_perms(self.owner, "reviewer_lead")
		self.client.force_login(self.owner)
		self._save(**{f"description_{self.session.id}": "mine now"})
		self.assertEqual(TimelapseAnnotation.objects.get().description, "old words")

	def test_editing_a_description(self):
		response = self._save(**{f"description_{self.session.id}": "new words"})
		self.assertEqual(TimelapseAnnotation.objects.get().description, "new words")

		edit = TimelapseReviewEdit.objects.get()
		self.assertEqual(edit.editor, self.lead)
		self.assertEqual(edit.note, "fixing a mistake")
		self.assertEqual(edit.changes["descriptions"], [
			{"session_id": self.session.id, "before": "old words", "after": "new words"},
		])
		log = AuditLog.objects.get(action="edit_timelapse_review")
		self.assertEqual(log.actor, self.lead)
		self.assertEqual(log.metadata["review_id"], self.review.id)
		self.assertContains(response, "new words")

	def test_a_description_cannot_be_blanked(self):
		self._save(**{f"description_{self.session.id}": "  "})
		self.assertEqual(TimelapseAnnotation.objects.get().description, "old words")
		self.assertFalse(TimelapseReviewEdit.objects.exists())

	def test_removing_a_deflation_keeps_a_copy_in_the_history(self):
		self._save(delete_removal=[str(self.original.id)])
		self.assertFalse(TimelapseRemoval.objects.exists())
		self.assertEqual(self.journal.approved_seconds, 180 * 60)

		changes = TimelapseReviewEdit.objects.get().changes
		self.assertEqual(changes["removals_deleted"][0]["reason"], "idle")
		self.assertEqual(changes["removals_deleted"][0]["end_seconds"], 30 * 60)
		self.assertEqual(changes["removed_seconds_before"], 30 * 60)
		self.assertEqual(changes["removed_seconds_after"], 0)

	def test_adding_a_deflation_counts_toward_makeup(self):
		with during_week(3):
			self._save(**self._add("0:40", "2:00"))
			added = TimelapseRemoval.objects.exclude(id=self.original.id).get()
			self.assertTrue(added.counts_toward_makeup)
			self.assertEqual(added.duration_seconds, 80 * 60)
			self.assertEqual(challenge.standing(self.owner).current.makeup_minutes, 80)
		self.slack_dm_mocks["timelapse_review"].assert_called_once()

	def test_removing_a_deflation_clears_its_makeup(self):
		with during_week(3):
			self._save(**self._add("0:40", "2:00"))
			added = TimelapseRemoval.objects.exclude(id=self.original.id).get()
			self._save(delete_removal=[str(added.id)])
			self.assertEqual(challenge.standing(self.owner).current.makeup_minutes, 0)

	def test_an_added_deflation_cannot_overlap_one_that_stays(self):
		response = self._save(**self._add("0:10", "0:50"))
		self.assertEqual(TimelapseRemoval.objects.count(), 1)
		self.assertTrue(any("overlaps a cut already" in m for m in message_texts(response)))

	def test_it_can_overlap_one_being_removed_in_the_same_edit(self):
		self._save(delete_removal=[str(self.original.id)], **self._add("0:10", "0:50"))
		self.assertEqual(TimelapseRemoval.objects.get().start_seconds, 10 * 60)

	def test_a_note_is_required(self):
		self._save(edit_note="", **{f"description_{self.session.id}": "new words"})
		self.assertEqual(TimelapseAnnotation.objects.get().description, "old words")

	def test_an_edit_that_changes_nothing_is_refused(self):
		response = self._save(**{f"description_{self.session.id}": "old words"})
		self.assertFalse(TimelapseReviewEdit.objects.exists())
		self.assertTrue(any("Nothing on the review" in m for m in message_texts(response)))

	def test_a_removal_from_another_review_cannot_be_deleted(self):
		other = make_journal(self.project, time_spent=60)
		other_session = other.timelapses.get()
		other_review = approve_timelapse(other, removals=[(other_session, 0, 600)])
		self._save(delete_removal=[str(other_review.removals.get().id)])
		self.assertEqual(TimelapseRemoval.objects.count(), 2)


class ReviewAuditUserTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.owner = make_user("owner", slack_id="U0OWNER", slack_username="sailor")
		self.project = make_project(self.owner, title="Boat")
		self.review = approve_timelapse(make_journal(self.project, title="Hull day"))
		someone = make_user("someone", slack_id="U0ELSE")
		self.other = approve_timelapse(make_journal(make_project(someone, title="Plane"), title="Wing day"))
		self.lead = grant_perms(make_user("lead", slack_id="U0LEAD"), "reviewer_lead")
		self.client.force_login(self.lead)
		AuditLog.objects.create(actor=self.lead, action="timelapse_review", target=f"Project #{self.project.id} (Boat)")
		AuditLog.objects.create(actor=self.lead, action="timelapse_review", target=f"Project #{self.other.journal.project_id} (Plane)")

	def test_searching_a_user_lists_their_lapse_reviews(self):
		response = self.client.get(reverse("review_audit"), {"user": "@sailor"})
		self.assertEqual([r.id for r in response.context["lapse_reviews"]], [self.review.id])
		self.assertContains(response, reverse("lapse_review_edit", args=[self.review.id]))
		self.assertNotContains(response, "Wing day")

	def test_searching_a_user_narrows_the_log_to_their_work(self):
		response = self.client.get(reverse("review_audit"), {"user": "owner"})
		self.assertEqual(
			[log.target for log in response.context["logs"]],
			[f"Project #{self.project.id} (Boat)"],
		)

	def test_no_search_lists_no_reviews(self):
		response = self.client.get(reverse("review_audit"))
		self.assertIsNone(response.context["lapse_reviews"])
		self.assertEqual(len(response.context["logs"]), 2)


class SaverLoanTests(BaseTestCase):
	"""Life preservers bought on credit by anyone with 20 approved hours."""

	def setUp(self):
		super().setUp()
		self.user = make_user("shipper", layers=0)
		self.project = make_project(self.user)
		self.client.force_login(self.user)
		self.saver = Item.objects.create(
			name="Life preserver", description="This week", cost=10,
			kind=Item.Kind.SAVER_CURRENT,
		)

	def _approve_hours(self, hours):
		journal = make_journal(self.project, time_spent=hours * 60)
		approve_timelapse(journal)

	def _buy(self, quantity=1, item=None, loan=True):
		# Ordering is rate limited, and some of these buy more than twice.
		cache.clear()
		data = {"quantity": str(quantity)}
		if loan:
			data["loan"] = "1"
		return self.client.post(
			reverse("order_item", args=[(item or self.saver).id]), data, follow=True
		)

	def _layers(self):
		return Profile.objects.get(user=self.user).layers

	def test_eligibility_counts_only_approved_time(self):
		make_journal(self.project, time_spent=25 * 60)  # waiting in the queue
		self.assertFalse(challenge.loan_eligible(self.user))
		journal = make_journal(self.project, time_spent=21 * 60)
		approve_timelapse(journal, removals=[(journal.timelapses.get(), 0, 2 * 3600)])
		# 21 hours tracked, 2 cut: 19 approved.
		self.assertEqual(challenge.approved_minutes(self.user), 19 * 60)
		self.assertFalse(challenge.loan_eligible(self.user))

	def test_an_eligible_user_can_buy_on_a_loan(self):
		self._approve_hours(20)
		with during_week(3):
			response = self._buy(quantity=2)
			self.assertEqual(SaverCredit.objects.filter(user=self.user).count(), 2)
		self.assertEqual(self._layers(), -20)
		self.assertEqual(Order.objects.get().loaned, 20)
		self.assertTrue(any("were a loan" in m for m in message_texts(response)))

	def test_only_the_missing_part_is_a_loan(self):
		self._approve_hours(20)
		Profile.objects.filter(user=self.user).update(layers=4)
		with during_week(3):
			self._buy()
		self.assertEqual(self._layers(), -6)
		self.assertEqual(Order.objects.get().loaned, 6)

	def test_a_loan_has_to_be_confirmed(self):
		self._approve_hours(20)
		with during_week(3):
			self._buy(loan=False)
			self.assertFalse(SaverCredit.objects.exists())
		self.assertEqual(self._layers(), 0)

	def test_an_ineligible_user_cannot_borrow(self):
		self._approve_hours(19)
		with during_week(3):
			response = self._buy()
			self.assertFalse(SaverCredit.objects.exists())
		self.assertEqual(self._layers(), 0)
		self.assertTrue(any("not have enough" in m for m in message_texts(response)))

	def test_at_most_five_savers_can_be_owed(self):
		self._approve_hours(20)
		with during_week(3):
			self._buy(quantity=4)
			response = self._buy(quantity=2)
			self.assertEqual(SaverCredit.objects.count(), 4)
			self._buy(quantity=1)
			self.assertEqual(SaverCredit.objects.count(), 5)
		self.assertEqual(self._layers(), -50)
		self.assertTrue(any("at most 5 life preservers" in m for m in message_texts(response)))

	def test_only_savers_can_be_bought_on_credit(self):
		self._approve_hours(20)
		sticker = Item.objects.create(name="Sticker", description="A sticker", cost=5)
		self._buy(item=sticker)
		self.assertFalse(Order.objects.exists())
		self.assertEqual(self._layers(), 0)

	def test_a_negative_balance_blocks_other_purchases(self):
		self._approve_hours(20)
		sticker = Item.objects.create(name="Sticker", description="A sticker", cost=5)
		with during_week(3):
			self._buy()
		self._buy(item=sticker, loan=False)
		self.assertEqual(Order.objects.filter(item=sticker).count(), 0)

	def test_the_shop_offers_the_loan(self):
		self._approve_hours(20)
		with during_week(3):
			response = self.client.get(reverse("shop"))
		items = {item.name: item for item in response.context["items"]}
		self.assertEqual(items["Life preserver"].loan_limit, 50)
		self.assertContains(response, "take this on a loan")

	def test_metrics_count_what_is_owed(self):
		self._approve_hours(20)
		make_user("saver-rich", slack_id="U0RICH", layers=100)
		with during_week(3):
			self._buy(quantity=3)
		organizer = grant_perms(make_user("org", slack_id="U0ORG"), "organizer")
		self.client.force_login(organizer)
		response = self.client.get(reverse("metrics"))
		users = response.context["users"]
		self.assertEqual(users["layers_owed"], 30)
		self.assertEqual(users["users_on_loan"], 1)
		self.assertEqual(users["layers_in_circulation"], 100)
