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

# What HQ's YSWS submission guidelines ask of a record before it goes into the
# unified database, as it applies to an Atlantis ship: the Printables listing
# is the Code URL, the editor model the Playable URL. A record that fails one of
# these gets the program fined at spot-check, and approving is what sends it.
T3_CHECKLIST = [
    {
        "key": "eligible",
        "label": "Eligible for the unified database",
        "detail": "Not a school assignment, not paid Hack Club work, and not a duplicate of a project already submitted. An update counts only the new work.",
    },
    {
        "key": "original",
        "label": "Original work, no signs of fraud",
        "detail": "Not copied or a lightly modified remix, and the journals and timelapses show real incremental work, not something manufactured after the fact.",
    },
    {
        "key": "code_url",
        "label": "Printables listing is public and open source",
        "detail": "It opens without logging in and has an open source license set.",
    },
    {
        "key": "reproducible",
        "label": "Someone else could rebuild it from the listing",
        "detail": "Modifiable CAD (.STEP, .F3D, not just .STL), plus a BOM and wiring diagram if it has electronics, and any build steps beyond printing.",
    },
    {
        "key": "playable_url",
        "label": "The editor model link works",
        "detail": "It downloads or opens publicly, and it's this project.",
    },
    {
        "key": "screenshot",
        "label": "The screenshot shows the actual project",
        "detail": "A still image of the model or the print. No GIFs, no video.",
    },
    {
        "key": "description",
        "label": "The description says what it is and what it's for",
        "detail": "A clear, short summary of the project's purpose and how it works.",
    },
    {
        "key": "hours",
        "label": "Airtable time is hours you're confident are real",
        "detail": "Proportional to the project's complexity. When in doubt, deflate; AI-generated work counts only the genuine effort around it.",
    },
    {
        "key": "justification",
        "label": "The justification would convince someone who wasn't here",
        "detail": "Specific technical features, a reason for any deflation, and evidence anyone can go check. Not \"looks good\".",
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
