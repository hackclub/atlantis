"""Guards for the dependency CVEs fixed by the pins in requirements.txt.

Every advisory below was remediated by a version bump, so the cheapest way to
keep it remediated is to fail loudly if an install ever drifts back under the
floor. The behavioural cases cover the fixes this project can actually reach,
so they break if a future release regresses one.
"""

import time

import cryptography
import django
import sqlparse
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import DomainNameValidator
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase
from django.utils.cache import has_vary_header
from sqlparse.exceptions import SQLParseError


def _version(dotted):
	return tuple(int(part) for part in dotted.split(".")[:3] if part.isdigit())


class DependencyFloorTests(SimpleTestCase):
	"""Minimum versions that keep the known advisories closed."""

	def test_cryptography_floor(self):
		# 48.0.1 vulnerable bundled OpenSSL (GHSA-537c-gmf6-5ccf)
		# 49.0.0 exponential cert path building (CVE-2026-69249)
		# 49.0.0 wildcard DNS escapes permittedSubtrees (CVE-2026-69248)
		# 50.0.0 PKCS#7 Bleichenbacher oracle (CVE-2026-69247)
		self.assertGreaterEqual(_version(cryptography.__version__), (50, 0, 0))

	def test_django_floor(self):
		# 6.0.6 signed cookie salt collision (CVE-2026-6873)
		# 6.0.6 STARTTLS partial connection reuse (CVE-2026-7666)
		# 6.0.6 case-sensitive Cache-Control (CVE-2026-8404)
		# 6.0.6 Vary: Authorization omitted (CVE-2026-35193)
		# 6.0.6 Vary whitespace padding (CVE-2026-48587)
		# 6.0.7 cached Set-Cookie response (CVE-2026-48588)
		# 6.0.7 GDALRaster heap over-read (CVE-2026-53877)
		# 6.0.7 DomainNameValidator newlines (CVE-2026-53878)
		self.assertGreaterEqual(_version(django.get_version()), (6, 0, 8))

	def test_sqlparse_floor(self):
		# 0.6.0 backslash SQL string breakout (CVE-2026-59894)
		# 0.6.0 dollar-quote ReDoS (CVE-2026-59893)
		# 0.6.0 TokenList O(subtree) CPU DoS (CVE-2026-54284)
		# 0.6.0 quadratic group_comments (CVE-2026-71491)
		self.assertGreaterEqual(_version(sqlparse.__version__), (0, 6, 0))


class SignedCookieSaltTests(SimpleTestCase):
	"""CVE-2026-6873: (name, salt) pairs used to collide when concatenated."""

	def test_legacy_salt_fallback_is_disabled(self):
		# We sign no cookies ourselves, so nothing needs the compatibility
		# window Django keeps open until 7.0.
		self.assertIs(settings.SIGNED_COOKIE_LEGACY_SALT_FALLBACK, False)

	def test_colliding_name_and_salt_is_rejected(self):
		response = HttpResponse()
		response.set_signed_cookie("a", "value", salt="bc")
		token = response.cookies["a"].value

		# ("a", salt="bc") and ("ab", salt="c") concatenate to the same string.
		collided = RequestFactory().get("/")
		collided.COOKIES["ab"] = token
		with self.assertRaises(Exception):
			collided.get_signed_cookie("ab", salt="c")

		# The matching (name, salt) still round-trips.
		correct = RequestFactory().get("/")
		correct.COOKIES["a"] = token
		self.assertEqual(correct.get_signed_cookie("a", salt="bc"), "value")


class VaryHeaderTests(SimpleTestCase):
	"""CVE-2026-48587: padded Vary values defeated has_vary_header()."""

	def test_padded_wildcard_is_recognised(self):
		response = HttpResponse()
		response["Vary"] = "  *  "
		self.assertTrue(has_vary_header(response, "*"))

	def test_padded_header_names_are_recognised(self):
		response = HttpResponse()
		response["Vary"] = " Cookie , Authorization "
		self.assertTrue(has_vary_header(response, "Cookie"))
		self.assertTrue(has_vary_header(response, "Authorization"))
		self.assertFalse(has_vary_header(response, "Accept"))


class DomainNameValidatorTests(SimpleTestCase):
	"""CVE-2026-53878: newlines in domains enabled header injection."""

	def test_newlines_are_rejected(self):
		validate = DomainNameValidator()
		for value in ("example.com\n", "example.com\r", "example.com\r\n", "example.com\nX-Evil: 1"):
			with self.subTest(value=value), self.assertRaises(ValidationError):
				validate(value)

	def test_ordinary_domain_still_valid(self):
		DomainNameValidator()("atlantis.hackclub.com")


class SqlparseDoSTests(SimpleTestCase):
	"""The sqlparse advisories were all CPU-exhaustion or escaping bugs."""

	# Generous enough to stay green on a loaded CI box; the vulnerable
	# versions blew past this by orders of magnitude.
	BUDGET_SECONDS = 5.0

	def _timed_parse(self, sql):
		started = time.perf_counter()
		try:
			sqlparse.parse(sql)
		except SQLParseError:
			# 0.6.0 answers hostile input with its depth/token caps, which is
			# the fix for CVE-2026-54284 rather than a failure.
			pass
		return time.perf_counter() - started

	def test_repeated_comments_are_linear(self):
		# CVE-2026-71491: quadratic group_comments.
		self.assertLess(self._timed_parse("select 1 " + "/*c*/ " * 4000), self.BUDGET_SECONDS)

	def test_deep_nesting_is_capped(self):
		# CVE-2026-54284: TokenList.__init__ materialised O(subtree) per group.
		sql = "select " + "(" * 4000 + "1" + ")" * 4000
		self.assertLess(self._timed_parse(sql), self.BUDGET_SECONDS)

	def test_unterminated_dollar_quote_is_linear(self):
		# CVE-2026-59893: ReDoS on dollar-quoted literals.
		self.assertLess(self._timed_parse("select $" + "a" * 6000 + "$"), self.BUDGET_SECONDS)

	def test_nesting_within_the_cap_still_parses(self):
		self.assertTrue(sqlparse.parse("select " + "(" * 50 + "1" + ")" * 50))

	def test_dollar_quoted_literal_still_parses(self):
		self.assertIn("$tag$body$tag$", str(sqlparse.parse("select $tag$body$tag$")[0]))

	def test_backslash_is_escaped_in_generated_snippets(self):
		# CVE-2026-59894: unescaped backslashes let the generated snippet break
		# out of its SQL string literal.
		sql = "select * from t where a = 'x\\'"
		for output_format in ("python", "php"):
			with self.subTest(output_format=output_format):
				self.assertIn("\\\\", sqlparse.format(sql, output_format=output_format))
