"""The inactivity checker, and what the review page does with what it finds.

The ffmpeg pass itself isn't exercised here — it needs a binary and a real
video, and the interesting logic is on either side of it: reading its log, and
turning the frames it flagged into segments a reviewer can be shown. Those are
the parts that can be wrong in a way nobody notices.
"""

from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from .. import activity
from ..models import LookoutSession
from .base import (
	BaseTestCase, grant_perms, make_journal, make_project, make_user,
)


class SegmentCollapsingTests(BaseTestCase):
	def test_consecutive_frames_become_one_segment(self):
		self.assertEqual(
			activity.collapse_into_segments([3, 4, 5]),
			[{"start": 3, "end": 6, "duration": 3}],
		)

	def test_a_gap_starts_a_new_segment(self):
		self.assertEqual(
			activity.collapse_into_segments([1, 2, 7, 8]),
			[
				{"start": 1, "end": 3, "duration": 2},
				{"start": 7, "end": 9, "duration": 2},
			],
		)

	def test_a_single_frame_is_one_second_long(self):
		"""Half-open at the end, like every other range in the review."""
		self.assertEqual(
			activity.collapse_into_segments([9]),
			[{"start": 9, "end": 10, "duration": 1}],
		)

	def test_unordered_and_repeated_frames_are_tolerated(self):
		self.assertEqual(
			activity.collapse_into_segments([5, 3, 4, 4]),
			[{"start": 3, "end": 6, "duration": 3}],
		)

	def test_nothing_flagged_is_no_segments(self):
		self.assertEqual(activity.collapse_into_segments([]), [])


class FfmpegOutputTests(BaseTestCase):
	LOG = """
	frame=   10 fps=0.0 q=-0.0 size=N/A time=00:00:10.00
	[Parsed_blackframe_2 @ 0x7f8] frame:4 pblack:100 pts:4 t:4.000000
	[Parsed_blackframe_2 @ 0x7f8] frame:5 pblack:99 pts:5 t:5.000000
	[Parsed_blackframe_2 @ 0x7f8] frame:6 pblack:100 pts:6 t:6.000000
	frame=   30 fps=0.0 q=-0.0 size=N/A time=00:00:30.00
	"""

	def test_reads_the_flagged_frames_and_the_total(self):
		total, inactive = activity.parse_output(self.LOG)
		self.assertEqual(total, 30)
		self.assertEqual(inactive, [4, 5, 6])

	def test_a_log_with_no_black_frames_is_all_activity(self):
		total, inactive = activity.parse_output("frame=   12 fps=0.0\n")
		self.assertEqual((total, inactive), (12, []))

	def test_reads_the_length_off_the_input_header(self):
		"""Truncated, so it agrees with the clock in the reviewer's player."""
		log = "  Duration: 00:01:10.93, start: 0.000000, bitrate: 120 kb/s\n"
		self.assertEqual(activity.parse_duration(log), 70)

	def test_a_length_over_an_hour_is_read_whole(self):
		log = "  Duration: 02:03:04.00, start: 0.000000\n"
		self.assertEqual(activity.parse_duration(log), 2 * 3600 + 3 * 60 + 4)

	def test_a_log_without_a_duration_reports_none_rather_than_zero(self):
		"""Unknown is not "empty" — a zero here would wipe the timeline."""
		self.assertIsNone(activity.parse_duration("frame=   12 fps=0.0\n"))

	def test_short_runs_are_dropped(self):
		"""One idle minute is normal; two in a row is a gap worth drawing."""
		log = (
			"frame=  100\n"
			"[Parsed_blackframe_0 @ 0x1] frame:2 pblack:100 pts:2 t:2.0\n"
			"[Parsed_blackframe_0 @ 0x1] frame:40 pblack:100 pts:40 t:40.0\n"
			"[Parsed_blackframe_0 @ 0x1] frame:41 pblack:100 pts:41 t:41.0\n"
		)
		with patch.object(activity, "run_ffmpeg", return_value=log):
			result = activity.analyse_file("/nowhere.mp4")

		self.assertEqual(result["segments"], [{"start": 40, "end": 42, "duration": 2}])
		self.assertEqual(result["inactive_frames"], 2)
		self.assertEqual(result["inactive_percentage"], 2.0)

	def test_an_ffmpeg_that_failed_reports_nothing_rather_than_guessing(self):
		with patch.object(activity, "run_ffmpeg", return_value=None):
			self.assertEqual(activity.analyse_file("/nowhere.mp4"), activity.empty_result())


class ActivityStorageTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.project = make_project(make_user("author"), shippable=True)
		self.journal = make_journal(self.project, time_spent=60)
		self.session = self.journal.timelapses.get()

	def test_a_finished_check_is_recorded_on_the_session(self):
		result = {
			"inactive_frames": 4,
			"total_frames": 61,
			"inactive_percentage": 6.7,
			"segments": [{"start": 10, "end": 14, "duration": 4}],
			"video_seconds": 61,
		}
		with patch.object(activity, "check_session", return_value=result):
			activity.check_and_store(self.session)

		self.session.refresh_from_db()
		self.assertEqual(self.session.inactive_frame_count, 4)
		self.assertEqual(self.session.inactive_percentage, 6.7)
		self.assertEqual(self.session.inactive_segments, result["segments"])
		self.assertTrue(self.session.activity_checked)
		# Four video seconds is four recorded minutes.
		self.assertEqual(self.session.inactive_seconds, 4)
		self.assertEqual(self.session.inactive_display, "4:00")
		# The pass is the only thing that ever measures the video, so what it
		# read is what the review page draws its timeline against from here.
		self.assertEqual(self.session.measured_video_seconds, 61)
		self.assertEqual(self.session.video_seconds, 61)

	def test_a_pass_that_could_not_read_a_length_leaves_the_estimate_alone(self):
		for unusable in (None, 0):
			with self.subTest(video_seconds=unusable):
				result = {**activity.empty_result(), "video_seconds": unusable}
				with patch.object(activity, "check_session", return_value=result):
					activity.check_and_store(self.session)

				self.session.refresh_from_db()
				self.assertIsNone(self.session.measured_video_seconds)
				self.assertEqual(
					self.session.video_seconds, self.session.estimated_video_seconds
				)

	def test_a_check_that_could_not_run_leaves_the_session_unchecked(self):
		"""Unreadable is not the same claim as clean, so nothing is written."""
		with patch.object(activity, "check_session", return_value=None):
			self.assertIsNone(activity.check_and_store(self.session))

		self.session.refresh_from_db()
		self.assertFalse(self.session.activity_checked)
		self.assertEqual(self.session.inactive_segments, [])

	def test_an_unchecked_session_is_not_a_clean_one(self):
		self.assertFalse(self.session.activity_checked)
		self.assertEqual(self.session.inactive_percentage, 0.0)


class ActivityOnTheReviewPageTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.client.force_login(grant_perms(make_user("tlrev"), "timelapse_review"))
		self.project = make_project(make_user("author"), shippable=True)
		self.journal = make_journal(self.project, time_spent=60)
		self.session = self.journal.timelapses.get()

	def _payload(self):
		response = self.client.get(
			reverse("timelapse_review_project", args=[self.project.id])
		)
		return response.context["payload"]

	def test_segments_reach_the_editor_in_video_seconds(self):
		LookoutSession.objects.filter(pk=self.session.pk).update(
			inactive_segments=[{"start": 10, "end": 14, "duration": 4}],
			inactive_percentage=6.7,
			activity_checked_at=timezone.now(),
		)

		recording = self._payload()["entries"][0]["recordings"][0]
		self.assertTrue(recording["activityChecked"])
		self.assertEqual(recording["inactivePercentage"], 6.7)
		self.assertEqual(recording["inactiveSegments"], [{"start": 10, "end": 14}])

	def test_an_unchecked_recording_says_so(self):
		recording = self._payload()["entries"][0]["recordings"][0]
		self.assertFalse(recording["activityChecked"])
		self.assertEqual(recording["inactiveSegments"], [])

	def test_the_page_counts_the_footage_nobody_has_analysed(self):
		response = self.client.get(
			reverse("timelapse_review_project", args=[self.project.id])
		)
		self.assertEqual(response.context["recording_count"], 1)
		self.assertEqual(response.context["unchecked_count"], 1)
