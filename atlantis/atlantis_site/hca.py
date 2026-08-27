import os
from datetime import date

from authlib.integrations.django_client import OAuth
from authlib.integrations.requests_client import OAuth2Session

HCA_METADATA_URL = "https://auth.hackclub.com/.well-known/openid-configuration"
HCA_SCOPE = "openid email name profile verification_status slack_id address birthdate"
USERINFO_TIMEOUT = 5

STORED_TOKEN_FIELDS = ("access_token", "refresh_token", "token_type", "expires_at", "scope")

# Every value HCA's `verification_status` claim can take (its Identity model
# computes it from the verifications on file). Only "verified" means the
# identity itself is signed off — "pending" is mid-review, "needs_submission"
# has nothing to review, and "ineligible" is a fatal rejection or a permaban.
VERIFICATION_VERIFIED = "verified"
VERIFICATION_PENDING = "pending"
VERIFICATION_NEEDS_SUBMISSION = "needs_submission"
VERIFICATION_INELIGIBLE = "ineligible"

oauth = OAuth()

oauth.register(
    name="hackclub",
    server_metadata_url=HCA_METADATA_URL,
    client_id=os.environ["HCA_CLIENT_ID"],
    client_secret=os.environ["HCA_CLIENT_SECRET"],
    client_kwargs={"scope": HCA_SCOPE},
)


class IdentityUnavailable(Exception):
    """HCA could not be asked about a user: no usable token on file, or the
    identity service refused or failed the request."""


class AddressUnavailable(IdentityUnavailable):
    """The address-shaped IdentityUnavailable. Kept as its own name because
    every caller that wants an address catches this one specifically."""

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


def extract_birthdate(userinfo):
    """The `birthdate` claim as an ISO date string, or "" if it isn't usable.

    Anything that isn't a real YYYY-MM-DD date is dropped rather than passed on:
    the only consumer is Airtable's Birthday column, which would reject a
    partial date (OIDC allows a bare year) and take an ambiguous one at face
    value.
    """
    if not isinstance(userinfo, dict):
        return ""

    identity = userinfo.get("identity")
    source = identity if isinstance(identity, dict) else userinfo

    raw = source.get("birthdate") or ""
    if not isinstance(raw, str):
        return ""
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError:
        return ""


def extract_verification(userinfo):
    """The (verification_status, ysws_eligible) pair HCA reports for a user.

    Both claims ride on the one `verification_status` scope, and they answer
    different questions: whether the identity is verified at all, and whether
    it is eligible for YSWS prizes (a verified alum who has aged out is not).

    ysws_eligible comes back tri-state — True or False once HCA has a verdict,
    None while it has none, which is where an identity with nothing submitted
    sits. Anything that isn't a real boolean is read as None rather than
    coerced, so a missing claim can't pass for a "no".
    """
    if not isinstance(userinfo, dict):
        return "", None

    identity = userinfo.get("identity")
    source = identity if isinstance(identity, dict) else userinfo

    status = source.get("verification_status")
    if not isinstance(status, str):
        status = ""

    eligible = source.get("ysws_eligible")
    if not isinstance(eligible, bool):
        eligible = None

    return status, eligible


def select_address(addresses, address_id=None):
    """The address matching address_id, else the primary, else the first."""
    if not addresses:
        return None
    if address_id:
        for address in addresses:
            if address.get("id") == address_id:
                return address
    for address in addresses:
        if address.get("primary"):
            return address
    return addresses[0]


def fetch_userinfo(profile):
    """Everything HCA will tell us about this user, in one request.

    Raises IdentityUnavailable if there is no usable token on file or the
    identity service can't be reached.
    """
    token = profile.get_hca_token()
    if not token:
        raise IdentityUnavailable("No Hack Club identity token on file")

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
        raise IdentityUnavailable(f"Identity fetch failed: {e}") from e
    finally:
        session.close()

    return userinfo


def refresh_verification(profile):
    """Re-ask HCA where this user stands and store the answer.

    Raises IdentityUnavailable if there is no usable token or HCA can't be
    reached, in which case the stored answer is left exactly as it was.
    """
    status, eligible = extract_verification(fetch_userinfo(profile))
    profile.save_verification(status, eligible)
    return status, eligible


def fetch_addresses(profile):
    try:
        return extract_addresses(fetch_userinfo(profile))
    except IdentityUnavailable as e:
        raise AddressUnavailable(str(e)) from e
