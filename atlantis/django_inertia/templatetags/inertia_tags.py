import json

from django import template
from django.utils.html import format_html

from ..settings import settings

register = template.Library()


@register.simple_tag(takes_context=True)
def inertia(context, app_id="app"):
    # Upstream read the hardcoded key "page__" and wrapped both arguments in
    # mark_safe, so the JSON payload landed in the single-quoted attribute
    # unescaped -- any prop containing an apostrophe (a Slack display name, a
    # project title) broke out of the attribute. Let format_html escape it.
    page = context[settings.INERTIA_PAGE_CONTEXT]
    return format_html('<div id="{}" data-page="{}"></div>', app_id, json.dumps(page))
