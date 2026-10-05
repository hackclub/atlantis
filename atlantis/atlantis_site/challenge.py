"""Streaks, savers, elimination and what an hour is worth.

weeks.py says when a week runs; this says what happened inside one. Nothing in
here is a cached verdict — a user's standing is recomputed from timelapses and
saver credits every time it is asked for, so a saver bought ten minutes ago
counts and no scheduled job has to have run for a page to be right.
WeekOutcome rows record that a week closed (and that its DM went out); they are
never consulted for whether it was met, only for an organizer's override.

Three numbers come out of here and they are deliberately different things:

  credited hours   tracked time plus saver hours, against the weekly five. What
                   the streak and elimination are judged on, and it uses
                   *tracked* time because internal timelapse review runs days
                   behind and a week has to settle on Sunday night. Weeks 1
                   and 2 are the exception: ten across the pair will do.
  printer hours    five per week survived, and no more however long you worked.
                   Forty of them buys a printer, which is exactly every week.
  pearls           paid at T3 finalization off *approved* time, split across
                   the weeks the work was done in. Nothing to do with either
                   bar; see payout_breakdown.
"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN

from django.db import transaction
from django.db.models import Count, F, IntegerField, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import weeks
from .models import (
	CHALLENGE_BASE_PEARLS_PER_HOUR, CHALLENGE_BONUS_PEARLS_PER_HOUR,
	PAYOUT_MULTIPLIER_DEFAULT, PEARLS_PER_HOUR, Item, PearlBracket, SaverCredit,
	Timelapse, TimelapseRemoval, WeekOutcome,
)


# The key the prep bucket goes under in every {week: minutes} map here. None,
# not 0: a week index is 1-based and "no week" has to be unmistakable.
PREP = None


def _logged_timelapses(user):
	"""Every recording of this user's that counts as logged work.

	Attached to a journal, because taping it into the book is what logging is —
	a Lookout session that was recorded and abandoned is not work anyone
	claimed. On a project that still exists, so hours can't be banked and the
	evidence then deleted.

	`at` is when the work happened: recorded_at, falling back to when the
	journal was written for legacy Lookout rows that never recorded one.
	"""
	return _all_logged_timelapses().filter(owner=user)


def _all_logged_timelapses():
	"""_logged_timelapses, for everyone at once."""
	return (
		Timelapse.objects
		.filter(journal__isnull=False, journal__project__deleted=False)
		.annotate(at=Coalesce("recorded_at", "journal__created_at"))
	)


def tracked_minutes_by_week(user):
	"""{week index or PREP: tracked minutes}, over everything this user logged.

	Bucketed in Python rather than SQL so that weeks.week_for stays the single
	answer to which week a moment is in — the boundaries are local midnights in
	a zone the database isn't told about, and a second implementation of that
	in SQL is a second thing to get wrong across the DST change.
	"""
	return _bucket_by_week(_logged_timelapses(user).values_list("at", "tracked_seconds"))


def _bucket_by_week(rows):
	"""{week index or PREP: minutes} from (moment, seconds) pairs."""
	totals = {}
	for at, seconds in rows:
		key = PREP if weeks.is_prep(at) else weeks.week_for(at)
		totals[key] = totals.get(key, 0) + (seconds or 0)
	return {key: seconds // 60 for key, seconds in totals.items()}


def saver_hours_by_week(user):
	"""{week index: hours} credited by savers, bought or granted.

	One row is one hour, so this is a count and not a sum of quantities.
	"""
	return dict(
		SaverCredit.objects.filter(user=user)
		.values_list("week_index")
		.order_by()
		.annotate(hours=Count("id"))
	)


def _overrides(user):
	return {
		row.week_index: row
		for row in WeekOutcome.objects.filter(user=user)
	}


@dataclass
class Week:
	"""One week's standing, as a page would show it."""
	index: int
	tracked_minutes: int = 0
	saver_hours: int = 0
	closed: bool = False
	current: bool = False
	override: str = ""
	# Weeks 1 and 2 are judged as a pair (weeks.GRACE_WEEKS). For a week in
	# the pair, grace_minutes is what the whole pair has credited and
	# grace_open says one of them hasn't closed yet.
	grace: bool = False
	grace_minutes: int = 0
	grace_open: bool = False

	@property
	def saver_minutes(self):
		return self.saver_hours * 60

	@property
	def credited_minutes(self):
		"""Time logged plus time bought, which is what the five is judged on."""
		return self.tracked_minutes + self.saver_minutes

	@property
	def required_minutes(self):
		"""What the bar is out of: the pair's ten for a grace week, else five."""
		return weeks.grace_minutes() if self.grace else weeks.WEEKLY_MINUTES

	@property
	def progress_minutes(self):
		"""What the bar is filled with, against required_minutes."""
		return self.grace_minutes if self.grace else self.credited_minutes

	@property
	def met(self):
		"""Whether the hours are there — an organizer's word beats the sum.

		Five in the week always does it. A grace week is also met once the
		pair has its ten between them, so a light week 1 is carried by a heavy
		week 2 and the other way round.
		"""
		if self.override == WeekOutcome.Override.PASS:
			return True
		if self.override == WeekOutcome.Override.FAIL:
			return False
		if self.credited_minutes >= weeks.WEEKLY_MINUTES:
			return True
		return self.grace and self.grace_minutes >= weeks.grace_minutes()

	@property
	def missed(self):
		"""Only a week that is over can be missed; a live one is just unfinished.

		A grace week isn't missed until the whole pair has closed: week 1 short
		is still recoverable all through week 2.
		"""
		return self.closed and not self.met and not (self.grace and self.grace_open)

	@property
	def pending(self):
		"""Closed short, but waiting on the rest of its grace pair."""
		return self.closed and not self.met and not self.missed

	@property
	def shortfall_minutes(self):
		"""Minutes that would make this week met.

		For a grace week that's whichever is nearer — its own five, or the
		pair's ten — so a saver is never sold for an hour the pair didn't need.
		"""
		if self.met:
			return 0
		short = weeks.WEEKLY_MINUTES - self.credited_minutes
		if self.grace:
			short = min(short, weeks.grace_minutes() - self.grace_minutes)
		return max(short, 0)

	@property
	def left_minutes(self):
		"""What the bar still needs, which is what a page says is "to go"."""
		return max(self.required_minutes - self.progress_minutes, 0)

	@property
	def done(self):
		"""The bar is full. For a grace week that's the pair's ten, not met."""
		if self.override:
			return self.met
		return self.progress_minutes >= self.required_minutes

	@property
	def shortfall_hours(self):
		"""Saver hours needed to rescue this week, rounding a part-hour up."""
		return -(-self.shortfall_minutes // 60)

	@property
	def percent(self):
		"""How full the weekly bar is, 0-100 and never over."""
		return min(round(self.progress_minutes / self.required_minutes * 100), 100)

	@property
	def progress_display(self):
		return format_minutes(self.progress_minutes)

	@property
	def left_display(self):
		return format_minutes(self.left_minutes)

	@property
	def required_hours(self):
		return self.required_minutes // 60

	@property
	def grace_due_later(self):
		"""A grace week before the last: its countdown isn't when the ten are due."""
		return self.grace and self.index != weeks.grace_weeks()[-1]

	@property
	def grace_deadline(self):
		"""When the pair's ten are actually due — the last grace week's end."""
		return weeks.deadline(weeks.grace_weeks()[-1])

	@property
	def grace_deadline_display(self):
		return _local_display(self.grace_deadline, "%a %b %-d, %-I:%M %p")

	@property
	def grace_label(self):
		""""weeks 1 and 2", the way a sentence names the pair."""
		return "weeks " + " and ".join(str(i) for i in weeks.grace_weeks())

	@property
	def credited_display(self):
		return format_minutes(self.credited_minutes)

	@property
	def shortfall_display(self):
		return format_minutes(self.shortfall_minutes)

	@property
	def tracked_display(self):
		return format_minutes(self.tracked_minutes)

	@property
	def label(self):
		return weeks.week_label(self.index)

	@property
	def deadline(self):
		return weeks.deadline(self.index)

	@property
	def deadline_display(self):
		""""Sun 11:59 PM", in the program's zone — TIME_ZONE is UTC, so a
		template's |date would print Monday morning."""
		return _local_display(self.deadline, "%a %-I:%M %p")


@dataclass
class Standing:
	"""Everything about where one user is in the program."""
	weeks: list = field(default_factory=list)
	prep_minutes: int = 0
	started: bool = False
	ended: bool = False

	@property
	def current(self):
		return next((w for w in self.weeks if w.current), None)

	@property
	def missed(self):
		"""Closed weeks that weren't met, oldest first."""
		return [w for w in self.weeks if w.missed]

	@property
	def earliest_missed(self):
		return self.missed[0] if self.missed else None

	@property
	def eliminated(self):
		"""Out of the program: any week has closed without its five hours.

		Derived, never stored. Buying enough savers for every missed week puts
		this back to False the instant the last one is paid for.
		"""
		return bool(self.missed)

	@property
	def survived(self):
		"""Closed weeks that were met — each one worth five printer hours."""
		return [w for w in self.weeks if w.closed and w.met]

	@property
	def printer_minutes(self):
		"""Progress towards the printer: five hours a week, capped at the total.

		A week credits its five whether it was met with logged time or rescued
		with savers, and credits no more than five however much was really
		logged. The week in progress counts what's in it so far so the bar
		moves with the work — but not while eliminated, when nothing banks
		until the missed weeks are paid for.
		"""
		banked = len(self.survived) * weeks.WEEKLY_MINUTES
		if not self.eliminated:
			# A grace week waiting on its pair counts what it has, like the
			# live one does, so week 1's bar doesn't empty at midnight.
			for week in self.weeks:
				if week.current or week.pending:
					banked += min(week.credited_minutes, weeks.WEEKLY_MINUTES)
		return min(banked, weeks.printer_hours() * 60)

	@property
	def printer_hours(self):
		return self.printer_minutes // 60

	@property
	def printer_percent(self):
		return min(round(self.printer_minutes / (weeks.printer_hours() * 60) * 100), 100)

	@property
	def printer_unlocked(self):
		"""Enough hours banked to claim a printer, and still in the program."""
		return not self.eliminated and self.printer_minutes >= weeks.printer_hours() * 60

	@property
	def can_claim_printer(self):
		"""Claiming opens once the program is over and the bar is full."""
		return self.ended and self.printer_unlocked

	@property
	def saver_hours_needed(self):
		"""Savers needed to rescue the earliest missed week and get back in.

		When that's a grace week, the pair missed together and is rescued
		together, so it's what both need.
		"""
		week = self.earliest_missed
		if week is None:
			return 0
		if week.grace:
			return sum(w.shortfall_hours for w in self.missed if w.grace)
		return week.shortfall_hours

	@property
	def saver_hours_outstanding(self):
		"""Savers that would rescue every missed week — the most worth buying.

		Past this, a missed-week saver has nowhere to go, so the shop refuses
		the order rather than taking pearls for hours that can't be applied.
		"""
		return sum(week.shortfall_hours for week in self.missed)


def standing(user, now=None):
	"""Where `user` stands right now — the one entry point everything reads."""
	now = now or timezone.now()
	overrides = {index: row.override for index, row in _overrides(user).items()}
	return _build_standing(
		tracked_minutes_by_week(user), saver_hours_by_week(user), overrides, now
	)


def standings(now=None):
	"""{user id: Standing} for everyone who has logged time or held a saver.

	The same answer standing() gives, in three queries rather than three per
	user, for the pages that need the whole field at once. Anyone who has done
	neither is left out: they have no hours to judge, and counting every
	account that ever signed in would have every week "missed" by people who
	never started.
	"""
	tracked, savers, overrides = _ledger()
	return _standings_for(tracked.keys() | savers.keys(), tracked, savers, overrides, now)


def standings_for(user_ids, now=None):
	"""{user id: Standing} for exactly these users, in three queries.

	Unlike standings(), someone who has logged nothing still gets a row — the
	empty one standing() would have given them.
	"""
	tracked, savers, overrides = _ledger()
	return _standings_for(user_ids, tracked, savers, overrides, now)


def _ledger():
	"""Everyone's ({week: tracked minutes}, {week: saver hours}, {week: override}),
	each keyed by user id."""
	rows = {}
	for owner, at, seconds in _all_logged_timelapses().values_list("owner", "at", "tracked_seconds"):
		rows.setdefault(owner, []).append((at, seconds))
	tracked = {owner: _bucket_by_week(pairs) for owner, pairs in rows.items()}

	savers = {}
	for owner, index, hours in (
		SaverCredit.objects.values_list("user", "week_index")
		.order_by()
		.annotate(hours=Count("id"))
	):
		savers.setdefault(owner, {})[index] = hours

	overrides = {}
	for owner, index, value in WeekOutcome.objects.values_list("user", "week_index", "override"):
		overrides.setdefault(owner, {})[index] = value

	return tracked, savers, overrides


def _standings_for(user_ids, tracked, savers, overrides, now):
	now = now or timezone.now()
	calendar = _calendar(now)
	return {
		owner: _build_standing(
			tracked.get(owner, {}), savers.get(owner, {}), overrides.get(owner, {}), now,
			calendar,
		)
		for owner in user_ids
	}


def _calendar(now):
	"""The parts of a Standing that are the same for everyone at `now`.

	Worked out once per page rather than once per user: on the pages that
	build the whole field, recomputing the week boundaries for every row was
	most of the time spent.
	"""
	return {
		"closed": set(weeks.closed_weeks(now)),
		"live": weeks.current_week(now),
		"grace": weeks.grace_weeks(),
		"started": weeks.has_started(now),
		"ended": weeks.has_ended(now),
	}


def _build_standing(tracked, savers, overrides, now, calendar=None):
	"""A Standing from one user's {week: minutes}, {week: saver hours} and
	{week: override}."""
	calendar = calendar or _calendar(now)
	closed = calendar["closed"]
	live = calendar["live"]
	grace = calendar["grace"]
	grace_minutes = sum(tracked.get(i, 0) + savers.get(i, 0) * 60 for i in grace)
	grace_open = any(i not in closed for i in grace)

	return Standing(
		weeks=[
			Week(
				index=index,
				tracked_minutes=tracked.get(index, 0),
				saver_hours=savers.get(index, 0),
				closed=index in closed,
				current=index == live,
				override=overrides.get(index, ""),
				grace=index in grace,
				grace_minutes=grace_minutes if index in grace else 0,
				grace_open=grace_open,
			)
			for index in range(1, weeks.week_count() + 1)
		],
		prep_minutes=tracked.get(PREP, 0),
		started=calendar["started"],
		ended=calendar["ended"],
	)


# ---- gates ---------------------------------------------------------------

# What someone who's been dropped is told, wherever the deck or the shop says so.
def elimination_reason(user, now=None):
	"""Why this user is out of the program, or "" if they aren't.

	Being out is a status, not a wall: journaling and shipping stay open so the
	pearls for the savers can still be earned. Empty before the program opens,
	when nothing can have been missed yet.
	"""
	state = standing(user, now)
	if not state.started or not state.eliminated:
		return ""

	week = state.earliest_missed
	hours = state.saver_hours_needed
	if week.grace:
		closed = (
			f"{week.grace_label.capitalize()} closed with "
			f"{format_minutes(week.grace_minutes)} of the "
			f"{weeks.grace_minutes() // 60} hours needed between them"
		)
	else:
		closed = (
			f"{week.label} closed with {format_minutes(week.credited_minutes)} "
			f"of the {weeks.WEEKLY_HOURS} hours needed"
		)
	return (
		f"You're out of the program: {closed}. Buy {hours} missed-week streak "
		f"saver{'s' if hours != 1 else ''} in the shop to get back in. You can "
		"still journal and ship while you're out, so your ships can earn the "
		"pearls to pay for them."
	)


def shipping_blocked_reason(user, now=None):
	"""Why shipping is closed to this user, or "".

	Only the end of the program closes it. Being out doesn't: shipping is the
	only way to earn pearls, and savers cost pearls, so blocking it would leave
	someone with an empty wallet no way back in.
	"""
	state = standing(user, now)
	if state.started and state.ended:
		return (
			"The eight weeks are over, so shipping is closed. Your printer claim "
			"is on the printer charts."
		)
	return ""


def _local_display(moment, fmt):
	return moment.astimezone(weeks.zone()).strftime(fmt)


def format_minutes(minutes):
	minutes = int(minutes or 0)
	return f"{minutes // 60}h {minutes % 60}m"


# ---- savers --------------------------------------------------------------

class SaverError(Exception):
	"""A saver can't be applied, with a sentence saying why."""


def target_week(user, kind, now=None):
	"""Which week a saver of `kind` would land on, or raise SaverError.

	The current-week saver wants a week in progress; the missed-week saver
	wants the oldest week that closed short. Neither invents one.
	"""
	state = standing(user, now)
	if kind == Item.Kind.SAVER_CURRENT:
		live = state.current
		if live is None:
			if not state.started:
				raise SaverError("The challenge weeks haven't started yet.")
			raise SaverError("The challenge weeks are over.")
		return live.index
	if kind == Item.Kind.SAVER_PAST:
		week = state.earliest_missed
		if week is None:
			raise SaverError("You haven't missed a week, so there's nothing to restore.")
		return week.index
	raise SaverError("That item isn't a streak saver.")


@transaction.atomic
def apply_saver(user, kind, hours=1, order=None, now=None):
	"""Credit `hours` saver hours and return the week index each one landed on.

	The missed-week saver re-picks its target for every hour: rescuing a week
	that was three short with five savers spends three on it and rolls the
	other two onto the next missed week, which is what someone buying five at
	once plainly means. The current-week saver stays on the live week
	throughout.
	"""
	if hours < 1:
		raise SaverError("Quantity must be at least one.")

	applied = []
	for _ in range(hours):
		index = target_week(user, kind, now)
		SaverCredit.objects.create(
			user=user,
			week_index=index,
			source=SaverCredit.Source.PURCHASE,
			order=order,
		)
		applied.append(index)

	return applied


def grant_saver_hours(user, week_index, hours, granted_by, note=""):
	"""An organizer putting hours on a week by hand."""
	SaverCredit.objects.bulk_create([
		SaverCredit(
			user=user,
			week_index=week_index,
			source=SaverCredit.Source.ADMIN,
			granted_by=granted_by,
			note=note,
		)
		for _ in range(hours)
	])


# ---- pearls --------------------------------------------------------------

def _approved_seconds_by_week(journals):
	"""{week or PREP: approved seconds} for a set of journals.

	Approved, not tracked: this feeds pearls, and time a timelapse reviewer cut
	is not paid for. Removals hang off a review's individual recordings, so each
	recording's approved time is its own — which is what lets the total be split
	by *when each recording was made* rather than smeared over the journal.
	"""
	removed = dict(
		TimelapseRemoval.objects
		.filter(review__journal__in=journals)
		.values_list("session")
		.order_by()
		.annotate(total=Sum(F("end_seconds") - F("start_seconds"), output_field=IntegerField()))
	)

	totals = {}
	rows = (
		Timelapse.objects.filter(journal__in=journals)
		.annotate(at=Coalesce("recorded_at", "journal__created_at"))
		.values_list("id", "at", "tracked_seconds")
	)
	for pk, at, seconds in rows:
		approved = max((seconds or 0) - removed.get(pk, 0), 0)
		if not approved:
			continue
		key = PREP if weeks.is_prep(at) else weeks.week_for(at)
		totals[key] = totals.get(key, 0) + approved
	return totals


def _apportion(total, shares):
	"""Split `total` across `shares` in proportion, losing nothing.

	Largest remainder: floor every share, then hand the leftover out to whoever
	was rounded down hardest. Without it a payout would quietly shrink by a few
	minutes each time it was split.
	"""
	weight = sum(shares.values())
	if not weight or total <= 0:
		return {}

	exact = {key: total * value / weight for key, value in shares.items()}
	out = {key: int(value) for key, value in exact.items()}
	left = total - sum(out.values())
	for key in sorted(exact, key=lambda k: exact[k] - out[k], reverse=True)[:left]:
		out[key] += 1
	return out


def _tenths(minutes):
	"""Minutes as whole tenths of an hour — the six-minute bucket payouts use."""
	return Decimal(minutes // 6)


@dataclass
class PayoutLine:
	"""One row of the breakdown a reviewer sees before they finalize."""
	week: object  # a week index, or PREP
	minutes: int
	rate: Decimal
	pearls: Decimal

	@property
	def label(self):
		if self.week is PREP:
			return "Prep weeks"
		return f"Week {self.week}"

	@property
	def is_prep(self):
		return self.week is PREP


def payout_breakdown(journals, payout_minutes, multiplier=PAYOUT_MULTIPLIER_DEFAULT, brackets=None):
	"""What `payout_minutes` of work on `journals` is worth, and why.

	The reviewer types one number for a whole ship, and that ship can cover work
	from the prep weeks and several challenge weeks at three different rates. So
	the minutes are handed out across the weeks the work was actually recorded
	in, in proportion to each week's approved time, and each week's share is
	then priced on its own terms:

	  prep            the flat rate, as it always was
	  first five      whatever is left of that week's base-rate allowance
	  everything else the bonus rate

	`brackets` is {week: minutes already paid at base rate}; leaving it out
	prices the ship as though nothing had been paid yet, which is what the
	preview on an unfinalized ship wants. Returns (pearls, lines, drawn), where
	`drawn` is the base-rate minutes this payout would consume per week — what
	the caller writes back to PearlBracket when it commits.
	"""
	brackets = brackets or {}
	approved = _approved_seconds_by_week(journals)
	if not approved:
		return 0, [], {}

	split = _apportion(int(payout_minutes), {k: v for k, v in approved.items()})

	total = Decimal(0)
	lines = []
	drawn = {}

	# Prep first, then weeks in order: the breakdown reads as a timeline.
	for key in sorted(split, key=lambda k: -1 if k is PREP else k):
		minutes = split[key]
		if minutes <= 0:
			continue

		if key is PREP:
			pearls = _tenths(minutes) * PEARLS_PER_HOUR / 10 * Decimal(multiplier)
			total += pearls
			lines.append(PayoutLine(PREP, minutes, PEARLS_PER_HOUR, pearls))
			continue

		room = max(weeks.WEEKLY_MINUTES - brackets.get(key, 0), 0)
		base_minutes = min(minutes, room)
		bonus_minutes = minutes - base_minutes
		if base_minutes:
			drawn[key] = base_minutes

		for rate, part in (
			(CHALLENGE_BASE_PEARLS_PER_HOUR, base_minutes),
			(CHALLENGE_BONUS_PEARLS_PER_HOUR, bonus_minutes),
		):
			if not part:
				continue
			pearls = _tenths(part) * rate / 10 * Decimal(multiplier)
			total += pearls
			lines.append(PayoutLine(key, part, rate, pearls))

	# Quantized once at the end rather than per line, so splitting a payout
	# across weeks can't cost the shipper a pearl to rounding.
	return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)), lines, drawn


def brackets_for(user):
	"""{week: minutes already paid at the base rate} for this user."""
	return dict(
		PearlBracket.objects.filter(user=user).values_list("week_index", "minutes_paid")
	)


def draw_brackets(user, drawn):
	"""Record base-rate minutes a finalized payout just consumed.

	Called inside the finalization transaction; the rows are locked by the same
	select_for_update that holds the profile, so two ships finalizing at once
	can't both spend the same allowance.
	"""
	for index, minutes in drawn.items():
		row, _ = PearlBracket.objects.select_for_update().get_or_create(
			user=user, week_index=index
		)
		row.minutes_paid = min(row.minutes_paid + minutes, weeks.WEEKLY_MINUTES)
		row.save(update_fields=["minutes_paid"])
