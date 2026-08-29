"""Server-side Inertia.js adapter.

Vendored from django-inertia 1.3.0 (MIT, Samuel Girardin,
https://github.com/girardinsamuel/django-inertia). That release is from May
2022 and pins `django<5`, so pip refuses to resolve it against our Django 6
pin. The code itself only touches long-stable Django APIs, so we vendor it
rather than fork it. Deliberate changes from upstream:

  * the asset version is resolved per request instead of once per process, so
    a Vite rebuild actually invalidates clients on a long-running server
    (see `middleware.InertiaMiddleware`);
  * the `dotty-dict` dependency is replaced with a small dotted-key lookup,
    since `get_shared_props(key=...)` was the only thing that needed it;
  * mutable default arguments on `render()` are gone.
"""

import html
from inspect import signature
from typing import Any, Callable

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

from .props import LazyProp, StaticProp
from .settings import settings

# Attribute the middleware stashes the per-request asset version on.
VERSION_ATTR = "_inertia_version"


def load_callable_props(d, request):
    for k, v in d.items():
        if isinstance(v, dict):
            load_callable_props(v, request)
        elif callable(v):
            # evaluate prop and pass request if prop accept it
            if len(signature(v).parameters) > 0:
                d[k] = v(request)
            else:
                d[k] = v()
        elif isinstance(v, LazyProp):
            if len(signature(v.callable).parameters) > 0:
                d[k] = v(request)
            else:
                d[k] = v()
        elif isinstance(v, StaticProp):
            d[k] = v()


def dotted_get(data, key, default=None):
    """Look up a dotted key path in nested dicts (replaces `dotty-dict`)."""
    current = data
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


# Straightforward implementation of the Singleton Pattern
class Inertia(object):
    _instance = None
    shared_props = {}
    rendered_template = ""
    _version = ""

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Inertia, cls).__new__(cls)
            # Put any initialization here.
            cls._instance.options = {
                "root_view": settings.INERTIA_ROOT_VIEW,
                "page_context": settings.INERTIA_PAGE_CONTEXT,
            }
            cls._instance.check_config()

        return cls._instance

    def check_config(self):
        if not self.options.get("root_view"):
            raise ImproperlyConfigured(
                "No Inertia template found. Either set INERTIA_ROOT_VIEW"
                "in settings.py or pass template parameter."
            )

    @classmethod
    def render(cls, request, component, props=None, view_data=None, custom_root_view=None):
        self = cls()
        page_data = self.get_page_data(request, component, props=props or {})

        if request.headers.get("X-Inertia", False):
            response = JsonResponse(page_data)
            response["X-Inertia"] = True
            response["Vary"] = "Accept"
            return response

        template = custom_root_view if custom_root_view else self.options.get("root_view")
        page_context = self.options.get('page_context')

        page_data = {page_context: page_data} if not view_data else \
            {**view_data, **{page_context: page_data}}

        return render(
            request,
            template,
            page_data,
        )

    @classmethod
    def location(cls, url):
        response = HttpResponse(status=409)
        response["X-Inertia-Location"] = url
        return response

    @staticmethod
    def lazy(callable: Callable):
        return LazyProp(callable)

    @staticmethod
    def static(value: Any):
        return StaticProp(value)

    def get_page_data(self, request, component, props):
        # merge shared props with page props, shared props keys takes precedence
        all_props = {**props, **self.get_shared_props()}
        # get props to use here if partial loading is requested
        props = self.get_props_to_use(request, all_props, component)
        # finally lazy load props and make request available to props being lazy loaded
        load_callable_props(props, request)

        page_data = {
            "component": self.get_component(component),
            "props": props,
            "url": request.get_full_path_info(),
            "version": self.get_version(request),
        }

        return page_data

    def get_shared_props(self, key=None, default=None):
        """Get all Inertia shared props or the one with the given key."""
        if key:
            return dotted_get(self.shared_props, key, default)
        else:
            return self.shared_props

    @classmethod
    def version(cls, version):
        self = cls()
        self._version = version
        return self

    @classmethod
    def get_version(cls, request=None):
        """The asset version for this request.

        Upstream cached this on the singleton, so the first request to hit a
        worker pinned the version for the life of the process. The middleware
        now resolves it per request and stashes it on the request; the
        class-level value stays as a fallback for calls made outside a
        request (and for anyone still using `Inertia.version(...)`).
        """
        if request is not None:
            version = getattr(request, VERSION_ATTR, None)
            if version is not None:
                return str(version() if callable(version) else version)
        self = cls()
        version = self._version
        if callable(version):
            version = version()
        return str(version)

    @classmethod
    def share(cls, key, value=None):
        self = cls()
        if isinstance(key, dict):
            self.shared_props = {**self.shared_props, **key}
        else:
            self.shared_props.update({key: value})
        return self

    def flush_shared(self):
        self.shared_props = {}

    def get_props_to_use(self, request, all_props, component):
        """Get props to return to the page:
        - when partial reload, required return 'only' props
        - add adapter props along view props (errors, message, auth ...)"""

        # partial reload feature
        only_props_header = request.headers.get("X-Inertia-Partial-Data")
        partial_component_header = request.headers.get("X-Inertia-Partial-Component") or {
            "name": ""
        }
        is_partial = only_props_header and partial_component_header == component
        props = {}

        if is_partial:
            only_props = only_props_header
            for key in all_props:
                # always load static props
                if key in only_props or isinstance(all_props[key], StaticProp):
                    props.update({key: all_props[key]})
        else:
            for prop_key, value in all_props.items():
                if not isinstance(value, LazyProp):
                    props.update({prop_key: value})

        return props

    def get_component(self, component):
        # TODO: check if escaping before here is needed
        return html.escape(component)
