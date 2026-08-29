from django_inertia.middleware import InertiaMiddleware

from . import inertia


class FalloutInertiaMiddleware(InertiaMiddleware):
    """Inertia middleware, but shared props are injected per request by
    `fallout_site.inertia.render` (the django-inertia default is a leaky
    process-wide singleton). We only keep the version check + PATCH→303."""

    def share(self, request):
        return {}

    def version(self, request):
        return inertia.asset_version()