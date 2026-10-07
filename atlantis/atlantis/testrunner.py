from django.test import override_settings
from django.test.runner import DiscoverRunner


class AtlantisTestRunner(DiscoverRunner):
	"""The default runner, with the real Airtable credentials taken away.

	settings.py loads .env unconditionally, so a test run would otherwise pick
	up the production PAT and base. Approving a T3 calls submit_ship inline, so
	any test that posts an approval creates a real record in the live base.
	Blanking the credentials here makes that impossible for every test, present
	and future; submit_ship treats an unconfigured Airtable as a failed
	submission rather than an error, so nothing needs to know about this.

	Tests that exercise the Airtable path set their own dummy credentials with
	override_settings, which still takes precedence over this.

	The autojoin channels go the same way: every Slack-linked signup invites
	somebody to them, and a test login shouldn't reach the real workspace.

	RUN_BACKGROUND_INLINE makes views.helpers.run_in_background call its job
	on the spot, so a test that asserts on a DM sees it sent by the time the
	response comes back rather than racing a pool thread for it.
	"""

	def setup_test_environment(self, **kwargs):
		super().setup_test_environment(**kwargs)
		self._airtable_guard = override_settings(
			AIRTABLE_PAT="", AIRTABLE_BASE_ID="", AIRTABLE_TABLE_ID="",
			AIRTABLE_EMAILS_TABLE_ID="",
			SLACK_AUTOJOIN_CHANNEL_IDS=[],
			RUN_BACKGROUND_INLINE=True,
		)
		self._airtable_guard.enable()

	def teardown_test_environment(self, **kwargs):
		self._airtable_guard.disable()
		super().teardown_test_environment(**kwargs)
