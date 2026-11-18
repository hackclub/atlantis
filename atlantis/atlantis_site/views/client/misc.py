from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.http import Http404
from django.views.decorators.http import require_POST

from ... import challenge, weeks
from ...hca import AddressUnavailable
from ...models import Item, Journal, Order, PrinterClaim, Profile, Timelapse, Ship
from ...printers import item_key, printer as find_printer, tracks, track as find_track
from ..helpers import rate_limit, record_audit, too_long, ysws_block_reason

# The one list of guides: the scroll rail in _guides_base.html renders it, and
# guide_detail() will only serve a slug that appears here. Adding a guide means
# adding a row plus templates/atlantis_site/guides/<slug>.html — no new route.
GUIDES = (
    {
        "slug": "intro",
        "title": "Intro",
        "blurb": "What Atlantis is, how the eight weeks run, and what CAD means here.",
    },
    {
        "slug": "faq",
        "title": "FAQ",
        "blurb": "Who can join, what counts, which printer you get, and who owns your design.",
    },
    {
        "slug": "cad-software",
        "title": "CAD Software",
        "blurb": "The approved packages, and how Fusion, Onshape, and Solidworks compare.",
    },
    {
        "slug": "designing-for-3dp",
        "title": "Designing for 3D Printing",
        "blurb": "Walls, overhangs, holes, tolerances, fillets, and orientation: designing parts that actually print.",
    },
    {
        "slug": "project-guidelines",
        "title": "Project Guidelines",
        "blurb": "What makes a project good enough to pass review, and what is disallowed.",
    },
    {
        "slug": "shipping",
        "title": "What Is Shipping?",
        "blurb": "What it means to ship a project, and how to ship the same one twice.",
    },
)

GUIDE_SLUGS = frozenset(guide["slug"] for guide in GUIDES)


def _guide_profile(request):
    # The guides read the same whether or not anyone is signed in, so these two
    # views are the public exception on this module. A logged-out reader has no
    # profile and no deck to go back to; _guides_base.html handles both.
    if request.user.is_authenticated:
        return request.user.hackclub_profile
    return None


def guides(request):
    return render(request, "atlantis_site/guides.html", {
        "profile": _guide_profile(request),
        "guides_nav": GUIDES,
        "active_guide": None,
    })


def guide_detail(request, slug):
    # Templates are picked from the registry rather than straight off the URL,
    # so a made-up slug is a 404 and never a template path to go hunting for.
    if slug not in GUIDE_SLUGS:
        raise Http404("No such guide")

    return render(request, f"atlantis_site/guides/{slug}.html", {
        "profile": _guide_profile(request),
        "guides_nav": GUIDES,
        "active_guide": slug,
    })

def _claim_context(user):
    """The state every printer page reads: hours banked, and what they buy.

    Nothing is bought while the program runs — the maps are a plan you can
    change your mind about for eight weeks — so all of this is about whether
    the one claim at the end is open yet and what it would cost.
    """
    state = challenge.standing(user)
    profile = user.hackclub_profile
    return {
        "profile": profile,
        "standing": state,
        "chosen_track": profile.printer_track,
        "claim": PrinterClaim.objects.filter(user=user).first(),
        # Eligibility last, so HCA is only re-asked about someone who could
        # otherwise buy. claim_printer asks again; this only hides the buttons.
        "claim_open": state.can_claim_printer and not ysws_block_reason(user),
        "printer_hours_total": weeks.printer_hours(),
    }


@login_required
def printer_select(request):
    # The chart room: every track's constellation at once, each one a way in
    # to the tree that printer_track() draws.
    context = _claim_context(request.user)
    return render(request, "atlantis_site/printer_select.html", {
        "tracks": tracks(),
        **context,
    })


@login_required
def printer_track(request, slug):
    # Same guard as guide_detail: the slug has to name a track we know, so a
    # made-up one is a 404 rather than a blank map.
    chosen = find_track(slug)
    if chosen is None:
        raise Http404("No such printer track")

    context = _claim_context(request.user)
    # What each star costs against what they can actually pay, so the map can
    # say which ones are within reach rather than leaving them to do the sums.
    affordable = context["profile"].layers
    printers = [
        {**entry, "affordable": entry["pearls"] <= affordable}
        for entry in chosen["printers"]
    ]

    return render(request, "atlantis_site/printer_track.html", {
        "track": {**chosen, "printers": printers},
        **context,
    })


@login_required
@require_POST
@rate_limit("choose_printer_track", 2)
def choose_printer_track(request, slug):
    """Commit to a manufacturer's tree — reversible right up until the claim.

    Free to change because nothing has been spent: the pearls for a printer and
    every upgrade leading to it are debited once, at the claim, so switching
    tracks costs nothing and refunds nothing.
    """
    if find_track(slug) is None:
        raise Http404("No such printer track")

    if PrinterClaim.objects.filter(user=request.user).exists():
        messages.error(request, "You've already claimed your printer.")
        return redirect("printer_track", slug=slug)

    profile = request.user.hackclub_profile
    profile.printer_track = slug
    profile.save(update_fields=["printer_track"])

    record_audit(request, "choose_printer_track", target=slug, metadata={"track": slug})
    messages.success(request, f"{find_track(slug)['name']} is your track. You can still change it any time before you claim.")
    return redirect("printer_track", slug=slug)


@login_required
@require_POST
@rate_limit("claim_printer", 3)
def claim_printer(request, slug):
    """Cash 40 hours and a pile of pearls in for one printer, once.

    This is the only moment anything on a tree is paid for. The cost is the
    whole path: the track's entry pearls plus every upgrade step between its
    opening printer and the one being claimed, which is exactly the `pearls`
    total printers.py works out. What comes out the other side is an ordinary
    pending Order, so fulfillment posts a printer the same way it posts
    filament.
    """
    printer_name = request.POST.get("printer", "").strip()
    chosen = find_track(slug)
    if chosen is None:
        raise Http404("No such printer track")

    entry = find_printer(slug, printer_name)
    if entry is None:
        messages.error(request, "That printer isn't on this chart.")
        return redirect("printer_track", slug=slug)

    state = challenge.standing(request.user)
    if not state.settled:
        # Not the end of the last week but the end of the buffer after it: until
        # then someone short on it isn't out yet, and could buy on the way out.
        opens = weeks.settled_at(state.extension_days).astimezone(weeks.zone())
        messages.error(
            request,
            f"Printers go on sale {opens:%a %b} {opens.day} at {opens:%-I:%M %p} Eastern, "
            "once the eight weeks are over.",
        )
        return redirect("printer_track", slug=slug)
    if state.eliminated:
        messages.error(request, challenge.elimination_reason(request.user))
        return redirect("printer_track", slug=slug)
    # A printer is a YSWS prize like any other: HCA has to say this person is
    # eligible, now and not just when they shipped.
    ineligible = ysws_block_reason(request.user)
    if ineligible:
        messages.error(request, f"Buying a printer needs a YSWS-eligible Hack Club identity. {ineligible}")
        return redirect("printer_track", slug=slug)
    # Only hours recorded inside the eight weeks count, five a week: prep
    # time, before or after, doesn't, however much of it there is.
    total = weeks.printer_hours()
    if not state.all_shipped:
        messages.error(
            request,
            f"You need all {total} hours from the eight weeks shipped to pass, "
            f"and you've shipped {state.shipped_printer_hours}.",
        )
        return redirect("printer_track", slug=slug)
    if not state.printer_unlocked:
        messages.error(
            request,
            f"All {total} of your hours from the eight weeks have to be finalized "
            f"to claim, and {state.finalized_printer_hours} are so far. You can "
            "claim once the rest of your ships finish review.",
        )
        return redirect("printer_track", slug=slug)

    # Resolved before the transaction: this calls out to HCA, which has no
    # business happening while row locks are held. The claim still goes through
    # if it fails — fulfillment resolves the primary address anyway.
    try:
        address_id = request.user.hackclub_profile.primary_address_id
    except AddressUnavailable:
        address_id = ""
    if too_long(address_id, Order, "address_id"):
        address_id = ""

    cost = entry["pearls"]

    with transaction.atomic():
        if PrinterClaim.objects.select_for_update().filter(user=request.user).exists():
            messages.error(request, "You've already claimed your printer.")
            return redirect("printer_track", slug=slug)

        profile = Profile.objects.select_for_update().get(user=request.user)
        if profile.layers < cost:
            messages.error(
                request,
                f"{entry['name']} costs {cost} pearls and you have {profile.layers}.",
            )
            return redirect("printer_track", slug=slug)

        item = Item.objects.filter(printer_key=item_key(slug, entry["name"])).first()
        if item is None:
            # The tree and the generated rows have drifted, which is an
            # operator problem and not something to charge anyone for.
            messages.error(request, "That printer isn't set up for claiming yet. Ask in #atlantis-help.")
            return redirect("printer_track", slug=slug)

        profile.layers -= cost
        # The claim fixes the track for good, whatever was picked before.
        profile.printer_track = slug
        profile.save(update_fields=["layers", "printer_track"])

        order = Order.objects.create(
            owner=request.user,
            item=item,
            quantity=1,
            cost=cost,
            address_id=address_id,
            user_notes=f"{chosen['name']}: {entry['name']}"[:100],
        )
        PrinterClaim.objects.create(
            user=request.user,
            track_slug=slug,
            printer_name=entry["name"],
            pearls_spent=cost,
            order=order,
        )

    record_audit(request, "claim_printer", target=f"{chosen['name']} {entry['name']}", metadata={
        "track": slug,
        "printer": entry["name"],
        "pearls": cost,
        "order_id": order.id,
    })
    messages.success(request, f"{entry['name']} bought! It's with fulfillment now.")
    return redirect("printer_track", slug=slug)

@login_required
def user_profile(request, user_id):
    profile = request.user.hackclub_profile
    user_viewed = get_object_or_404(get_user_model(), id=user_id)
    viewed_profile = user_viewed.hackclub_profile
    is_self = user_viewed == request.user

    projects = user_viewed.projects.filter(deleted=False)
    if not is_self and not request.user.has_perm("atlantis_site.organizer"):
        projects = projects.exclude(locked=True)
    projects = projects.order_by("id")

    journals = Journal.objects.filter(project__in=projects).select_related("project").order_by("-created_at")
    journal_count = journals.count()
    ship_count = Ship.objects.filter(project__in=projects).count()
    # Tracked, not approved: approved/removed seconds come from timelapse
    # review, which is internal and never shown back to the person it's about.
    tracked_seconds = Timelapse.objects.filter(project__in=projects).aggregate(
        total=Sum("tracked_seconds")
    )["total"] or 0

    return render(request, "atlantis_site/user.html", {
        "profile": profile,
        "user_viewed": user_viewed,
        "viewed_profile": viewed_profile,
        "projects": projects,
        "journals": journals[:12],
        "journal_count": journal_count,
        "ship_count": ship_count,
        "tracked_hours": tracked_seconds // 3600,
        "tracked_minutes": (tracked_seconds % 3600) // 60,
        "is_self": is_self,
    })
