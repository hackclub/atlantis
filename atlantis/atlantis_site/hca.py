import os

from authlib.integrations.django_client import OAuth
from authlib.integrations.requests_client import OAuth2Session

HCA_METADATA_URL = "https://auth.hackclub.com/.well-known/openid-configuration"
HCA_SCOPE = "openid email name profile verification_status slack_id address"
USERINFO_TIMEOUT = 5

STORED_TOKEN_FIELDS = ("access_token", "refresh_token", "token_type", "expires_at", "scope")

oauth = OAuth()

oauth.register(
    name="hackclub",
    server_metadata_url=HCA_METADATA_URL,
    client_id=os.environ["HCA_CLIENT_ID"],
    client_secret=os.environ["HCA_CLIENT_SECRET"],
    client_kwargs={"scope": HCA_SCOPE},
)


class AddressUnavailable(Exception):
    """HCA could not be asked for an address: no usable token on file, or the
    identity service refused or failed the request."""

def storable_token(token):
    if not token or not token.get("access_token"):
        return {}
    return {field: token[field] for field in STORED_TOKEN_FIELDS if token.get(field)}


def extract_addresses(userinfo):
    if not isinstance(userinfo, dict):
        return []

    identity = userinfo.get("identity")
    source = identity if isinstance(identity, dict) else userinfo

    raw = source.get("addresses")
    if isinstance(raw, dict):
        raw = [raw]
    if not raw:
        single = source.get("address")
        raw = [single] if isinstance(single, dict) and single else []

    return [
        {k: v for k, v in address.items() if k != "phone_number"}
        for address in raw
        if isinstance(address, dict)
    ]


def fetch_addresses(profile):
    token = profile.get_hca_token()
    if not token:
        raise AddressUnavailable("No Hack Club identity token on file")

    metadata = oauth.hackclub.load_server_metadata()
    token_endpoint = metadata["token_endpoint"]
    userinfo_endpoint = metadata["userinfo_endpoint"]

    session = OAuth2Session(
        client_id=oauth.hackclub.client_id,
        client_secret=oauth.hackclub.client_secret,
        token=token,
        token_endpoint=token_endpoint,
        update_token=lambda new_token, **kwargs: profile.save_hca_token(new_token),
    )

    try:
        response = session.get(userinfo_endpoint, timeout=USERINFO_TIMEOUT)
        if response.status_code == 401 and token.get("refresh_token"):
            session.refresh_token(token_endpoint, refresh_token=token["refresh_token"])
            response = session.get(userinfo_endpoint, timeout=USERINFO_TIMEOUT)
        response.raise_for_status()
        userinfo = response.json()
    except Exception as e:
        raise AddressUnavailable(f"Address fetch failed: {e}") from e
    finally:
        session.close()

    return extract_addresses(userinfo)
