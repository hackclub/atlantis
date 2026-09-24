from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from .. import weeks
from ..printers import ENTRY_HOURS, TRACKS, track, tracks
from .base import BaseTestCase, make_user

ASSETS = Path(settings.BASE_DIR) / "atlantis_site/static/atlantis_site/assets/img/printer"


class PrinterCostTests(BaseTestCase):
	"""The tech-tree arithmetic, which is the part a reader can't eyeball."""

	def _costs(self, slug):
		return {p["name"]: p["pearls"] for p in track(slug)["printers"]}

	def test_entry_printer_costs_hours_alone(self):
		self.assertEqual(self._costs("bambu")["A1 Mini"], 0)

	def test_upgrades_accumulate_down_a_branch(self):
		costs = self._costs("bambu")
		# A1 Mini -> A1 (240) -> P1S (290) -> P2S (440)
		self.assertEqual(costs["A1"], 240)
		self.assertEqual(costs["P1S"], 530)
		self.assertEqual(costs["P2S"], 970)

	def test_branches_do_not_pay_for_each_other(self):
		costs = self._costs("elegoo")
		# Both hang off Neptune 4 Pro (70), so each pays only its own step.
		self.assertEqual(costs["Neptune 4 Plus"], 470)
		self.assertEqual(costs["Centauri Carbon 2"], 290)

	def test_track_entry_pearls_are_added_to_every_printer(self):
		# Qidi's 40 hours stop short of its cheapest printer, so its whole
		# tree carries the 390-pearl shortfall.
		costs = self._costs("qidi")
		self.assertEqual(costs["Q2C"], 390)
		self.assertEqual(costs["Q2"], 700)

	def test_label_names_hours_and_pearls(self):
		labels = {p["name"]: p["cost"] for p in track("elegoo")["printers"]}
		self.assertEqual(labels["Neptune 4"], f"{ENTRY_HOURS} hours")
		self.assertEqual(labels["Neptune 4 Pro"], f"{ENTRY_HOURS} hours + 70 pearls")

	def test_every_printer_is_reachable_from_the_entry(self):
		for spec in TRACKS:
			names = {name for name, *_ in spec["printers"]}
			roots = [name for name, parent, *_ in spec["printers"] if parent is None]
			with self.subTest(spec["slug"]):
				self.assertEqual(len(roots), 1, "a track needs exactly one entry printer")
				for name, parent, *_ in spec["printers"]:
					if parent is not None:
						self.assertIn(parent, names, f"{name} upgrades from nothing we know")

	def test_star_positions_sit_on_the_map(self):
		for spec in TRACKS:
			for name, _parent, _pearls, x, y in spec["printers"]:
				with self.subTest(track=spec["slug"], printer=name):
					self.assertTrue(0 <= x <= 100, f"{name} is off the map horizontally")
					self.assertTrue(0 <= y <= 100, f"{name} is off the map vertically")

	def test_every_track_has_its_artwork(self):
		for spec in TRACKS:
			with self.subTest(spec["slug"]):
				self.assertTrue((ASSETS / f"map_{spec['slug']}.png").is_file())
				self.assertTrue((ASSETS / f"card_{spec['slug']}.webp").is_file())


class PrinterSelectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user(layers=120)

	def test_login_required(self):
		self.assertEqual(self.client.get(reverse("printer_select")).status_code, 302)

	def test_renders_every_track(self):
		self.client.force_login(self.user)
		response = self.client.get(reverse("printer_select"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(len(response.context["tracks"]), len(TRACKS))
		for spec in TRACKS:
			self.assertContains(response, reverse("printer_track", args=[spec["slug"]]))

	def test_dashboard_links_here(self):
		self.client.force_login(self.user)
		response = self.client.get(reverse("dashboard"))
		self.assertContains(response, reverse("printer_select"))


class PrinterTrackTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user(layers=120)

	def test_login_required(self):
		self.assertEqual(
			self.client.get(reverse("printer_track", args=["bambu"])).status_code, 302
		)

	def test_unknown_track_404(self):
		self.client.force_login(self.user)
		response = self.client.get(reverse("printer_track", args=["prusa"]))
		self.assertEqual(response.status_code, 404)

	def test_draws_a_star_per_printer(self):
		self.client.force_login(self.user)
		for spec in TRACKS:
			with self.subTest(spec["slug"]):
				response = self.client.get(reverse("printer_track", args=[spec["slug"]]))
				self.assertEqual(response.status_code, 200)
				self.assertContains(
					response, 'class="star"', count=len(spec["printers"])
				)

	def test_hover_label_carries_name_and_cost(self):
		self.client.force_login(self.user)
		response = self.client.get(reverse("printer_track", args=["elegoo"]))
		self.assertContains(response, "Neptune 4 Pro")
		self.assertContains(response, f"{ENTRY_HOURS} hours + 70 pearls")

	def test_what_a_printer_costs_is_never_shown_in_dollars(self):
		"""A printer's cost is hours and pearls, never money.

		BYOP is the exception that proves it: the dollars on that track are
		the grant a tier hands you, and the cost of reaching it is still only
		ever hours and pearls.
		"""
		self.client.force_login(self.user)
		priced = {"byop"}
		pages = [reverse("printer_select")] + [
			reverse("printer_track", args=[spec["slug"]])
			for spec in TRACKS if spec["slug"] not in priced
		]
		for page in pages:
			with self.subTest(page):
				body = self.client.get(page).content.decode()
				self.assertNotIn("$", body)
				self.assertNotIn("USD", body)

	def test_byop_names_its_tiers_by_the_grant_they_carry(self):
		self.client.force_login(self.user)
		response = self.client.get(reverse("printer_track", args=["byop"]))
		self.assertContains(response, "$250 grant")
		self.assertContains(response, "$650 grant")
		# The grant is the budget, not the cost; the cost stays in pearls.
		self.assertContains(response, f"{ENTRY_HOURS} hours + 840 pearls")

	def test_the_chart_room_stays_free_of_dollars(self):
		"""Every card shows an entry cost, and none of them is a price."""
		self.client.force_login(self.user)
		body = self.client.get(reverse("printer_select")).content.decode()
		self.assertNotIn("$", body)

	def test_costed_tracks_match_the_tables(self):
		self.assertEqual(
			[t["slug"] for t in tracks()], [spec["slug"] for spec in TRACKS]
		)


@override_settings(CHALLENGE_WEEKS=8)
class PrinterPaceTests(BaseTestCase):
	"""The weekly-hours estimate: 2/hr for the required five, 7/hr on top.

	An 8-week season everyone survives banks 10 pearls a week from the required
	hours alone (80 pearls total) before a single bonus hour is worked, so a
	printer's pace is 5h/week up to that point and climbs from there.
	"""

	def _pearls(self, slug, name):
		return next(p["pearls"] for p in track(slug)["printers"] if p["name"] == name)

	def _pace(self, slug, name):
		return next(p["pace"] for p in track(slug)["printers"] if p["name"] == name)

	def test_a_printer_the_required_hours_alone_cover_costs_no_extra_time(self):
		# 0 pearls: the required five hours, every week, and nothing more.
		self.assertEqual(self._pearls("bambu", "A1 Mini"), 0)
		self.assertEqual(self._pace("bambu", "A1 Mini"), "~5.0h/week")

	def test_a_printer_past_the_free_eighty_costs_more_than_the_required_five(self):
		# A1 is 240 pearls: 160 of it has to come from bonus hours, spread
		# over 8 weeks at 7/hr — 160 / 56 = ~2.86 more hours than the five
		# that are already required.
		self.assertEqual(self._pearls("bambu", "A1"), 240)
		self.assertEqual(self._pace("bambu", "A1"), "~7.9h/week")

	def test_pace_climbs_with_pearl_cost_down_a_branch(self):
		costs = {p["name"]: p["pearls"] for p in track("bambu")["printers"]}
		paces = {p["name"]: p["pace"] for p in track("bambu")["printers"]}
		chain = ["A1 Mini", "A1", "P1S", "P2S"]
		self.assertEqual([costs[name] for name in chain], sorted(costs[name] for name in chain))
		hours = [float(paces[name].removeprefix("~").removesuffix("h/week")) for name in chain]
		self.assertEqual(hours, sorted(hours), "a pricier printer should never look cheaper")

	def test_a_track_whose_entry_already_costs_pearls_has_a_pace_above_the_floor(self):
		# Qidi's entry (390 pearls) costs more than the free 80, so even its
		# opening printer needs bonus hours: 310 / 56 = ~5.54, rounded up.
		self.assertEqual(self._pearls("qidi", "Q2C"), 390)
		self.assertEqual(self._pace("qidi", "Q2C"), "~10.6h/week")

	def test_the_pace_never_rounds_down_past_what_it_actually_costs(self):
		"""Following the shown pace every week must never come up short.

		Decimal's default rounding is banker's rounding, which would take an
		estimate landing exactly on a tenth (11.25) down to 11.2 rather than up
		to 11.3 — a person doing exactly what the star says would end the
		season short of the pearls it actually costs. The estimate has to
		round the other way.
		"""
		for spec in TRACKS:
			for entry in track(spec["slug"])["printers"]:
				hours = Decimal(entry["pace"].removeprefix("~").removesuffix("h/week"))
				season = weeks.week_count()
				banked = (
					weeks.WEEKLY_HOURS * 2  # base rate
					+ (hours - weeks.WEEKLY_HOURS) * 7  # bonus rate
				) * season
				with self.subTest(track=spec["slug"], printer=entry["name"]):
					self.assertGreaterEqual(banked, entry["pearls"])

	def test_the_track_entry_pace_matches_its_own_opening_printer(self):
		for spec in TRACKS:
			opening = next(p for p in track(spec["slug"])["printers"] if p["pearls"] == min(
				pr["pearls"] for pr in track(spec["slug"])["printers"]
			))
			with self.subTest(spec["slug"]):
				self.assertEqual(track(spec["slug"])["entry_pace"], opening["pace"])

	def test_a_shorter_season_asks_for_more_hours_a_week(self):
		with override_settings(CHALLENGE_WEEKS=4):
			shorter = self._pace("bambu", "A1")
		self.assertNotEqual(shorter, self._pace("bambu", "A1"))

	def test_pace_appears_on_the_track_page(self):
		user = make_user(layers=1000)
		self.client.force_login(user)
		response = self.client.get(reverse("printer_track", args=["bambu"]))
		self.assertContains(response, "~7.9h/week")

	def test_pace_appears_in_the_claim_dropdown(self):
		user = make_user(layers=1000)
		self.client.force_login(user)
		response = self.client.get(reverse("printer_track", args=["bambu"]))
		self.assertContains(response, "240 pearls, ~7.9h/week")

	def test_pace_appears_on_the_chart_room(self):
		user = make_user(layers=1000)
		self.client.force_login(user)
		response = self.client.get(reverse("printer_select"))
		self.assertContains(response, "~5.0h/week")  # bambu's entry
		self.assertContains(response, "~10.6h/week")  # qidi's entry
