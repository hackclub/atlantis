import io
import ipaddress
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase

from slack_sdk.errors import SlackApiError

from ..models import (
	AuditLog,
	detect_editor,
	detect_editor_from_filename,
	detect_editor_from_link,
)
from ..views import helpers
from ..views.helpers import (
	add_bars,
	build_journal_timeline,
	display_name,
	get_client_ip,
	is_valid_editor_model_url,
	is_valid_image_url,
	is_valid_printables_url,
	is_valid_stl_url,
	layers_for_minutes,
	random_storage_key,
	record_audit,
	reviewer_leaderboard,
	send_slack_dm,
	sniff_image_extension,
	validate_file_size,
)
from .base import User, make_journal, make_project, make_ship, make_user, image_upload


class EditorDetectionTests(TestCase):
	def test_detect_editor_from_filename_known_extensions(self):
		self.assertEqual(detect_editor_from_filename("part.f3d"), "Fusion 360")
		self.assertEqual(detect_editor_from_filename("assembly.sldasm"), "Solidworks")
		self.assertEqual(detect_editor_from_filename("thing.FCStd"), "FreeCAD")
		self.assertEqual(detect_editor_from_filename("box.scad"), "OpenSCAD")
		self.assertEqual(detect_editor_from_filename("scene.blend"), "Blender")
		self.assertEqual(detect_editor_from_filename("sketch.slvs"), "Solvespace")
		self.assertEqual(detect_editor_from_filename("model.shapr"), "Shapr3D")

	def test_detect_editor_from_filename_is_case_insensitive(self):
		self.assertEqual(detect_editor_from_filename("PART.F3D"), "Fusion 360")

	def test_detect_editor_from_filename_unknown(self):
		self.assertIsNone(detect_editor_from_filename("model.stl"))
		self.assertIsNone(detect_editor_from_filename("no_extension"))

	def test_detect_editor_from_link_known_domains(self):
		self.assertEqual(detect_editor_from_link("https://onshape.com/doc/1"), "Onshape")
		self.assertEqual(detect_editor_from_link("https://cad.onshape.com/doc/1"), "Onshape")
		self.assertEqual(detect_editor_from_link("https://a360.co/abc"), "Fusion 360")
		self.assertEqual(detect_editor_from_link("https://myhub.autodesk360.com/x"), "Fusion 360")
		self.assertEqual(detect_editor_from_link("https://collab.shapr3d.com/x"), "Shapr3D")

	def test_detect_editor_from_link_rejects_lookalike_domains(self):
		self.assertIsNone(detect_editor_from_link("https://notonshape.com/doc"))
		self.assertIsNone(detect_editor_from_link("https://onshape.com.evil.com/doc"))
		self.assertIsNone(detect_editor_from_link("https://example.com/onshape.com"))

	def test_detect_editor_prefers_file_extension_in_url_path(self):
		self.assertEqual(detect_editor("https://cdn.example.com/files/part.f3d"), "Fusion 360")

	def test_detect_editor_falls_back_to_domain(self):
		self.assertEqual(detect_editor("https://cad.onshape.com/documents/abc"), "Onshape")

	def test_detect_editor_empty_values(self):
		self.assertIsNone(detect_editor(""))
		self.assertIsNone(detect_editor(None))
		self.assertIsNone(detect_editor("https://example.com/whatever"))


class UrlValidatorTests(TestCase):
	def test_valid_printables_urls(self):
		self.assertTrue(is_valid_printables_url("https://printables.com"))
		self.assertTrue(is_valid_printables_url("https://www.printables.com/model/123-thing"))
		self.assertTrue(is_valid_printables_url("HTTPS://WWW.PRINTABLES.COM/model/1"))

	def test_invalid_printables_urls(self):
		self.assertFalse(is_valid_printables_url("http://printables.com/model/1"))
		self.assertFalse(is_valid_printables_url("https://thingiverse.com/thing/1"))
		self.assertFalse(is_valid_printables_url("https://evil.com/https://printables.com"))
		self.assertFalse(is_valid_printables_url(""))

	def test_valid_editor_model_urls(self):
		self.assertTrue(is_valid_editor_model_url(
			"https://pub-d9ac82fd80854a42ae2dde2757ff0a55.r2.dev/editor_models/a.f3d"
		))
		self.assertTrue(is_valid_editor_model_url(
			"https://cdn.pub-d9ac82fd80854a42ae2dde2757ff0a55.r2.dev/x"
		))

	def test_invalid_editor_model_urls(self):
		self.assertFalse(is_valid_editor_model_url("https://evil.r2.dev/x"))
		self.assertFalse(is_valid_editor_model_url(
			"https://pub-d9ac82fd80854a42ae2dde2757ff0a55.r2.dev.evil.com/x"
		))
		self.assertFalse(is_valid_editor_model_url(""))


class LayersForMinutesTests(TestCase):
	def test_conversion_table(self):
		cases = {
			0: 0,
			5: 0,
			6: 0,  
			12: 1,
			29: 2,
			30: 2,
			36: 3,
			60: 5,
			120: 10,
			240: 20,
		}
		for minutes, layers in cases.items():
			with self.subTest(minutes=minutes):
				self.assertEqual(layers_for_minutes(minutes), layers)


class BuildJournalTimelineTests(TestCase):
	def setUp(self):
		self.user = make_user("timeline")
		self.project = make_project(self.user)

	def test_events_sorted_newest_first(self):
		j1 = make_journal(self.project, time_spent=60)
		ship = make_ship(self.project, journal_minutes=(90, 45))
		j2 = make_journal(self.project, time_spent=30)

		events = build_journal_timeline(
			self.project.journals.filter(ship__isnull=True), [ship]
		)

		self.assertEqual([e["type"] for e in events], ["journal", "ship", "journal"])
		keys = [e["sort_key"] for e in events]
		self.assertEqual(keys, sorted(keys, reverse=True))

	def test_ship_event_totals_journal_time(self):
		ship = make_ship(self.project, journal_minutes=(90, 45))
		events = build_journal_timeline([], [ship])
		self.assertEqual(events[0]["time_spent"], 135)
		self.assertEqual(events[0]["time_display"], "2h 15m")

	def test_ship_event_feedback_defaults_to_empty(self):
		ship = make_ship(self.project, journal_minutes=())
		events = build_journal_timeline([], [ship])
		self.assertEqual(events[0]["feedback"], "")

	def test_empty_inputs(self):
		self.assertEqual(build_journal_timeline([], []), [])


class GetClientIpTests(TestCase):
	def setUp(self):
		self.factory = RequestFactory()

	def test_uses_first_forwarded_ip(self):
		request = self.factory.get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 5.6.7.8")
		self.assertEqual(get_client_ip(request), "1.2.3.4")

	def test_falls_back_to_remote_addr(self):
		request = self.factory.get("/")
		self.assertEqual(get_client_ip(request), "127.0.0.1")


class RecordAuditTests(TestCase):
	def setUp(self):
		self.factory = RequestFactory()
		self.user = make_user("auditor")

	def _post_request(self, data=None, user=None):
		request = self.factory.post("/some/path/", data or {})
		request.user = user or self.user
		return request

	def test_creates_audit_log_with_form_data(self):
		request = self._post_request({"field": "value", "csrfmiddlewaretoken": "secret"})
		record_audit(request, "test_action", target="Thing #1", metadata={"a": 1})

		log = AuditLog.objects.get()
		self.assertEqual(log.actor, self.user)
		self.assertEqual(log.action, "test_action")
		self.assertEqual(log.target, "Thing #1")
		self.assertEqual(log.path, "/some/path/")
		self.assertEqual(log.method, "POST")
		self.assertEqual(log.metadata, {"a": 1})
		self.assertEqual(log.form_data, {"field": "value"})
		self.assertNotIn("csrfmiddlewaretoken", log.form_data)

	def test_multi_value_fields_stored_as_lists(self):
		request = self._post_request({"groups": ["1", "2"]})
		record_audit(request, "test_action")
		self.assertEqual(AuditLog.objects.get().form_data, {"groups": ["1", "2"]})

	def test_anonymous_actor_is_null(self):
		from django.contrib.auth.models import AnonymousUser
		request = self._post_request()
		request.user = AnonymousUser()
		record_audit(request, "anon_action")
		self.assertIsNone(AuditLog.objects.get().actor)

	def test_uploaded_file_names_recorded(self):
		upload = SimpleUploadedFile("photo.png", b"data")
		request = self.factory.post("/p/", {"image": upload})
		request.user = self.user
		record_audit(request, "upload_action")
		log = AuditLog.objects.get()
		self.assertEqual(log.form_data["_uploaded_files"], {"image": ["photo.png"]})

	def test_long_target_truncated(self):
		request = self._post_request()
		record_audit(request, "test_action", target="x" * 500)
		self.assertEqual(len(AuditLog.objects.get().target), 255)


class FileValidationTests(TestCase):
	def test_validate_file_size_boundaries(self):
		exactly_1mb = SimpleUploadedFile("f", b"\0" * (1024 * 1024))
		over_1mb = SimpleUploadedFile("f", b"\0" * (1024 * 1024 + 1))
		self.assertTrue(validate_file_size(exactly_1mb, 1))
		self.assertFalse(validate_file_size(over_1mb, 1))

	def test_sniff_image_extension_formats(self):
		self.assertEqual(sniff_image_extension(image_upload(fmt="PNG")), ".png")
		self.assertEqual(sniff_image_extension(image_upload(fmt="JPEG")), ".jpg")
		self.assertEqual(sniff_image_extension(image_upload(fmt="GIF")), ".gif")
		self.assertEqual(sniff_image_extension(image_upload(fmt="WEBP")), ".webp")

	def test_sniff_image_extension_rejects_non_images(self):
		fake = SimpleUploadedFile("fake.png", b"not an image at all")
		self.assertIsNone(sniff_image_extension(fake))

	def test_sniff_image_extension_rejects_disallowed_format(self):
		buf = io.BytesIO()
		from PIL import Image
		Image.new("RGB", (2, 2)).save(buf, format="BMP")
		bmp = SimpleUploadedFile("image.bmp", buf.getvalue())
		self.assertIsNone(sniff_image_extension(bmp))

	def test_sniff_resets_file_position(self):
		upload = image_upload()
		sniff_image_extension(upload)
		self.assertEqual(upload.tell(), 0)

	def test_random_storage_key_format(self):
		key = random_storage_key("images", ".png")
		self.assertRegex(key, r"^images/[0-9a-f]{32}\.png$")
		self.assertNotEqual(key, random_storage_key("images", ".png"))


class SsrfGuardTests(TestCase):
	"""Tests for the private-network guards around outbound HEAD requests."""

	def test_is_public_ip(self):
		public = ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"]
		private = ["10.0.0.1", "192.168.1.1", "172.16.0.1", "127.0.0.1",
				   "169.254.1.1", "224.0.0.1", "0.0.0.0", "::1"]
		for ip in public:
			self.assertTrue(helpers._is_public_ip(ipaddress.ip_address(ip)), ip)
		for ip in private:
			self.assertFalse(helpers._is_public_ip(ipaddress.ip_address(ip)), ip)

	@patch("layered_site.views.helpers.socket.getaddrinfo")
	def test_host_resolving_to_private_ip_rejected(self, mock_dns):
		mock_dns.return_value = [(None, None, None, None, ("127.0.0.1", 0))]
		self.assertFalse(helpers._host_resolves_to_public("internal.example.com"))

	@patch("layered_site.views.helpers.socket.getaddrinfo")
	def test_host_resolving_to_public_ip_accepted(self, mock_dns):
		mock_dns.return_value = [(None, None, None, None, ("93.184.216.34", 0))]
		self.assertTrue(helpers._host_resolves_to_public("example.com"))

	@patch("layered_site.views.helpers.socket.getaddrinfo")
	def test_unresolvable_host_rejected(self, mock_dns):
		import socket
		mock_dns.side_effect = socket.gaierror
		self.assertFalse(helpers._host_resolves_to_public("nope.invalid"))

	def test_empty_host_rejected(self):
		self.assertFalse(helpers._host_resolves_to_public(""))

	@patch("layered_site.views.helpers.socket.getaddrinfo")
	def test_ipv4_mapped_ipv6_unwrapped(self, mock_dns):
		mock_dns.return_value = [(None, None, None, None, ("::ffff:127.0.0.1", 0))]
		self.assertFalse(helpers._host_resolves_to_public("sneaky.example.com"))


def _response(status=200, headers=None, redirect=False):
	response = MagicMock()
	response.headers = headers or {}
	response.is_redirect = redirect
	response.is_permanent_redirect = False
	return response


@patch("layered_site.views.helpers._host_resolves_to_public", return_value=True)
class SafeHeadTests(TestCase):
	def test_rejects_non_http_schemes(self, _dns):
		self.assertIsNone(helpers._safe_head("ftp://example.com/file"))
		self.assertIsNone(helpers._safe_head("file:///etc/passwd"))
		self.assertIsNone(helpers._safe_head("not a url"))

	@patch("layered_site.views.helpers.requests.head")
	def test_returns_response_for_plain_url(self, mock_head, _dns):
		mock_head.return_value = _response(headers={"Content-Type": "image/png"})
		response = helpers._safe_head("https://example.com/a.png")
		self.assertIsNotNone(response)

	@patch("layered_site.views.helpers.requests.head")
	def test_follows_redirects_manually(self, mock_head, _dns):
		final = _response(headers={"Content-Type": "image/png"})
		hop = _response(headers={"Location": "https://example.com/real.png"}, redirect=True)
		mock_head.side_effect = [hop, final]
		self.assertIs(helpers._safe_head("https://example.com/a"), final)
		self.assertEqual(mock_head.call_count, 2)

	@patch("layered_site.views.helpers.requests.head")
	def test_gives_up_after_max_redirects(self, mock_head, _dns):
		hop = _response(headers={"Location": "https://example.com/loop"}, redirect=True)
		mock_head.return_value = hop
		self.assertIsNone(helpers._safe_head("https://example.com/a", max_redirects=3))

	@patch("layered_site.views.helpers.requests.head")
	def test_redirect_to_private_host_blocked(self, mock_head, _dns):
		_dns.side_effect = [True, False]
		hop = _response(headers={"Location": "http://169.254.169.254/meta"}, redirect=True)
		mock_head.return_value = hop
		self.assertIsNone(helpers._safe_head("https://example.com/a"))


class ContentTypeValidatorTests(TestCase):
	@patch("layered_site.views.helpers._safe_head")
	def test_is_valid_image_url(self, mock_head):
		mock_head.return_value = _response(headers={"Content-Type": "image/png"})
		self.assertTrue(is_valid_image_url("https://example.com/a.png"))

		mock_head.return_value = _response(headers={"Content-Type": "text/html"})
		self.assertFalse(is_valid_image_url("https://example.com/page"))

		mock_head.return_value = None
		self.assertFalse(is_valid_image_url("https://example.com/blocked"))

		mock_head.side_effect = Exception("boom")
		self.assertFalse(is_valid_image_url("https://example.com/error"))

	@patch("layered_site.views.helpers._safe_head")
	def test_is_valid_stl_url_content_types(self, mock_head):
		for content_type in ("model/stl", "model/x.stl-ascii", "model/x.stl-binary", "application/sla"):
			mock_head.return_value = _response(headers={"Content-Type": content_type})
			self.assertTrue(is_valid_stl_url("https://example.com/m"), content_type)

	@patch("layered_site.views.helpers._safe_head")
	def test_is_valid_stl_url_octet_stream_requires_stl_path(self, mock_head):
		mock_head.return_value = _response(headers={"Content-Type": "application/octet-stream"})
		self.assertTrue(is_valid_stl_url("https://example.com/model.STL"))
		self.assertFalse(is_valid_stl_url("https://example.com/model.zip"))

	@patch("layered_site.views.helpers._safe_head")
	def test_is_valid_stl_url_rejects_other_types(self, mock_head):
		mock_head.return_value = _response(headers={"Content-Type": "text/html"})
		self.assertFalse(is_valid_stl_url("https://example.com/model.stl"))


class SlackDmTests(TestCase):
	@patch("layered_site.views.helpers.slack_client.chat_postMessage")
	def test_returns_true_on_success(self, mock_post):
		self.assertTrue(send_slack_dm("hello", "U123"))
		mock_post.assert_called_once_with(channel="U123", text="hello")

	@patch("layered_site.views.helpers.slack_client.chat_postMessage")
	def test_returns_false_on_slack_error(self, mock_post):
		mock_post.side_effect = SlackApiError("nope", {"ok": False})
		self.assertFalse(send_slack_dm("hello", "U123"))


class GetModelInfoTests(TestCase):
	@patch("layered_site.views.helpers.requests.post")
	def test_returns_print_payload(self, mock_post):
		mock_post.return_value.json.return_value = {
			"data": {"print": {"id": "1", "makesCount": 3}}
		}
		self.assertEqual(helpers.get_model_info("1"), {"id": "1", "makesCount": 3})

	@patch("layered_site.views.helpers.requests.post")
	def test_raises_on_graphql_errors(self, mock_post):
		mock_post.return_value.json.return_value = {"errors": [{"message": "bad"}], "data": None}
		with self.assertRaises(ValueError):
			helpers.get_model_info("1")


class DisplayNameTests(TestCase):
	def test_none_user(self):
		self.assertEqual(display_name(None), "deleted user")

	def test_prefers_slack_username(self):
		user = make_user("subname", slack_username="cool-slack-name")
		self.assertEqual(display_name(user), "cool-slack-name")

	def test_falls_back_to_username_when_no_slack_name(self):
		user = make_user("subname2", slack_username="")
		self.assertEqual(display_name(user), "subname2")

	def test_falls_back_to_username_when_no_profile(self):
		user = User.objects.create_user(username="noprofile", password="pw")
		self.assertEqual(display_name(user), "noprofile")


class AddBarsTests(TestCase):
	def test_scales_to_largest_value(self):
		rows = add_bars([{"value": 10}, {"value": 5}, {"value": 0}])
		self.assertEqual([r["bar"] for r in rows], [100.0, 50.0, 0.0])

	def test_all_zero_values_do_not_divide_by_zero(self):
		rows = add_bars([{"value": 0}, {"value": 0}])
		self.assertEqual([r["bar"] for r in rows], [0.0, 0.0])

	def test_empty_rows(self):
		self.assertEqual(add_bars([]), [])

	def test_custom_value_key(self):
		rows = add_bars([{"n": 4}, {"n": 2}], value_key="n")
		self.assertEqual([r["bar"] for r in rows], [100.0, 50.0])


class ReviewerLeaderboardTests(TestCase):
	def test_ranks_reviewers_by_count(self):
		from ..models import T1
		author = make_user("author")
		project = make_project(author)
		busy = make_user("busy", slack_username="Busy Reviewer")
		quiet = make_user("quiet")
		idle = make_user("idle")

		for _ in range(3):
			ship = make_ship(project, journal_minutes=())
			T1.objects.create(ship=ship, reviewer=busy, feedback="", internal_notes="", approved=True)
		ship = make_ship(project, journal_minutes=())
		T1.objects.create(ship=ship, reviewer=quiet, feedback="", internal_notes="", approved=True)

		rows = reviewer_leaderboard("t1_reviews")
		self.assertEqual(rows[0], {"label": "Busy Reviewer", "value": 3, "bar": 100.0})
		self.assertEqual(rows[1]["value"], 1)
		self.assertNotIn("idle", [r["label"] for r in rows])

	def test_limit(self):
		from ..models import T1
		author = make_user("author")
		project = make_project(author)
		for i in range(4):
			reviewer = make_user(f"reviewer{i}")
			ship = make_ship(project, journal_minutes=())
			T1.objects.create(ship=ship, reviewer=reviewer, feedback="", internal_notes="", approved=True)
		self.assertEqual(len(reviewer_leaderboard("t1_reviews", limit=2)), 2)
