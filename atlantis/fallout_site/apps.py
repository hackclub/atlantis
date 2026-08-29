from django.apps import AppConfig


class FalloutSiteConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fallout_site"

    def ready(self):
        import fallout_site.signals  # noqa: F401