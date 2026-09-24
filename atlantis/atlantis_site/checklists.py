SHIP_CHECKLIST = [
    {
        "key": "step_file",
        "label": "A STEP file is on the Printables page",
        "detail": "A .step export, so the model can be opened and edited in any CAD program.",
    },
    {
        "key": "description",
        "label": "The description is detailed",
        "detail": "It says what the project is, what it's for, and how it works.",
    },
    {
        "key": "presentation",
        "label": "The page looks good",
        "detail": "Renders or photos of the model, and a page you'd be happy to see on the Printables front page.",
    },
    {
        "key": "license",
        "label": "It's under an open source license",
        "detail": "Every project on Atlantis has to be.",
    },
    {
        "key": "editor_file",
        "label": "The editor file here is the right one",
        "detail": "It's current, it opens, and it's the project you're shipping.",
    },
    {
        "key": "functional",
        "label": "The project does something/is mechanical.",
        "detail": "It's functional or mechanical, not just a decorative piece.",
    },
]

T1_CHECKLIST = [
    {
        "key": "description",
        "label": "Good description on the Printables page",
        "detail": "Detailed, and it actually describes this project rather than a sentence of filler.",
    },
    {
        "key": "models_present",
        "label": "Every model file is on the page, STEP included",
        "detail": "A .step export is there alongside the rest.",
    },
    {
        "key": "license",
        "label": "An open source license is set",
        "detail": "Check the listing's license field, not the description.",
    },
    {
        "key": "editor_file",
        "label": "The editor file opens and is the right one",
        "detail": "It loads, and what's in it is the project being shipped.",
    },
    {
        "key": "slices_clean",
        "label": "Slicing produces no unprintable parts",
        "detail": "Nothing that comes out of the slicer is impossible to print.",
    },
    {
        "key": "volume",
        "label": "Under 256cm³ in the slicer",
        "detail": "The sliced model's volume is below the limit.",
    },
    {
        "key": "functional",
        "label": "The project is functional or mechanical",
        "detail": "It's functional or mechanical, not just a decorative piece.",
    },
]

FIELD = "checklist"


def unticked(checklist, request):
    """The items of `checklist` this POST didn't confirm, in the order shown.

    An unknown key is simply not a tick for anything — the answer is built from
    the checklist rather than from what was sent, so a post can only ever be
    missing items, never invent them.
    """
    ticked = set(request.POST.getlist(FIELD))
    return [item for item in checklist if item["key"] not in ticked]


def ticked(checklist, request):
    """The keys of `checklist` this POST confirmed, in the order shown.

    Built from the checklist rather than from the post, so what gets recorded
    is always a subset of the list as it stands today — a stray value in the
    request can't write itself into the audit log.
    """
    sent = set(request.POST.getlist(FIELD))
    return [item["key"] for item in checklist if item["key"] in sent]


def unticked_message(items, lead):
    """`lead`, then the labels of what's still unticked, as one sentence."""
    labels = "; ".join(item["label"] for item in items)
    return f"{lead} {labels}"
