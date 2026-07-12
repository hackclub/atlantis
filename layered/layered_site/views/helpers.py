from django.contrib.auth.decorators import user_passes_test
from django.conf import settings
from django.contrib import messages
from ..models import AuditLog

from slack_sdk.errors import SlackApiError
from slack_sdk import WebClient

from urllib.parse import urlparse

from PIL import Image

import os
import uuid
import requests
import re

ALLOWED_IMAGE_FORMATS = {
    "PNG": ".png",
    "JPEG": ".jpg",  
    "GIF": ".gif",
    "WEBP": ".webp",
}

PRINTABLES_URL_RE = re.compile(r"https:\/\/(?:www\.)?printables\.com(?:\/.*)?", re.IGNORECASE)
CLOUDFLARE_BUCKET_RE = re.compile(r"^https?:\/\/(?:[a-zA-Z0-9-]+\.)*pub-d9ac82fd80854a42ae2dde2757ff0a55\.r2\.dev(?:\/.*)?$", re.IGNORECASE)

slack_client = WebClient(token=settings.SLACK_TOKEN, timeout=5)

def check_perms(perms):
    def check_perms_internal(user):
        for perm in perms:
            if user.has_perm(perm):
                return True
        return False
    return user_passes_test(check_perms_internal)

def is_valid_printables_url(value):
    return bool(PRINTABLES_URL_RE.match(value))

def layers_for_minutes(minutes):
    tenths_of_hour = minutes // 6
    return round(tenths_of_hour * 0.5)

def get_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")

def record_audit(request, action, target="", metadata=None):
    form_data = {
        key: request.POST.getlist(key) if len(request.POST.getlist(key)) > 1 else value
        for key, value in request.POST.items()
        if key != "csrfmiddlewaretoken"
    }
    if request.FILES:
        form_data["_uploaded_files"] = {
            field: [f.name for f in request.FILES.getlist(field)]
            for field in request.FILES
        }

    try:
        AuditLog.objects.create(
            actor=request.user if request.user.is_authenticated else None,
            action=action,
            target=str(target)[:255],
            path=request.path,
            method=request.method,
            ip_address=get_client_ip(request),
            form_data=form_data,
            metadata=metadata or {},
        )
    except Exception as e:
        messages.error(request, f"Failed to log audit: {e}")

def is_valid_image_url(url):
    try:
        result = urlparse(url)
        if result.scheme not in ('http', 'https') or not result.netloc:
            return False
        response = requests.head(url, allow_redirects=True, timeout=5)
        content_type = response.headers.get('Content-Type', '')
        return content_type.startswith('image/')
    except Exception:
        return False

def is_valid_stl_url(url):
    try:
        result = urlparse(url)
        if result.scheme not in ('http', 'https') or not result.netloc:
            return False
        response = requests.head(url, allow_redirects=True, timeout=5)
        content_type = response.headers.get('Content-Type', '')
        stl_content_types = ('model/stl', 'model/x.stl-ascii', 'model/x.stl-binary', 'application/sla')
        if any(content_type.startswith(ct) for ct in stl_content_types):
            return True
        if content_type.startswith('application/octet-stream') or not content_type:
            return result.path.lower().endswith('.stl')
        return False
    except Exception:
        return False

def get_model_info(model_id: str) -> dict:
    PRINTABLES_GRAPHQL_URL = os.environ['PRINTABLES_GRAPHQL_URL']
    QUERY = """
    query GetModelInfo($id: ID!) {
    print(id: $id) {
        id
        name
        slug
        makesCount
        license {
        id
        name
        disallowRemixing
        }
    }
    }
    """

    payload = {
        "operationName": "GetModelInfo",
        "variables": {"id": model_id},
        "query": QUERY,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.printables.com",
        "Referer": "https://www.printables.com/",
        "User-Agent": "Mozilla/5.0 (compatible; Layered/1.0)",
    }
    response = requests.post(PRINTABLES_GRAPHQL_URL, json=payload, headers=headers, timeout=5)
    response.raise_for_status()

    data = response.json()

    if "errors" in data:
        raise ValueError(f"GraphQL API errors: {data['errors']}")
    
    return data["data"]["print"]

def send_slack_dm(content, user):
    try:
        response = slack_client.chat_postMessage(
            channel=user,
            text=content
        )
        return True
    except SlackApiError:
        return False
    
def is_valid_editor_model_url(value):
    return bool(CLOUDFLARE_BUCKET_RE.match(value))

def validate_file_size(file, max_mb):
    max_b = max_mb * 1024 * 1024
    if file.size > max_b:
        return False
    return True

def sniff_image_extension(file):
    try:
        file.seek(0)
        image = Image.open(file)
        image_format = image.format
        image.verify()
    except Exception:
        return None
    finally:
        file.seek(0)
    return ALLOWED_IMAGE_FORMATS.get(image_format)

def random_storage_key(prefix, extension):
    return f"{prefix}/{uuid.uuid4().hex}{extension}"