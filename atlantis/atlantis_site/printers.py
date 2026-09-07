"""The printer tech trees, one per manufacturer track.

Everyone starts by logging `ENTRY_HOURS` hours, which buys the cheapest
printer on their track; from there each printer is unlocked by spending pearls
on the step that leads to it. A track's map under
assets/img/printer/map_<slug>.png draws that tree as a constellation, and the
`x`/`y` on each printer is the percentage position of its dot on that image —
the star overlay in printer_track.html is laid over the map at those points,
so the two have to be edited together.

Pearl figures are the per-step costs off the tech-tree diagrams. What a
printer costs in total is the sum of the steps leading to it plus the track's
entry pearls, which is what `tracks()` works out. What a printer *sells* for
is deliberately absent: the cost a person is shown is only ever hours and
pearls. The BYOP tiers are the exception, and not really one — the dollars in
their names are the grant each tier hands you, not a price it charges.
"""

ENTRY_HOURS = 40

# Each printer is (name, parent, pearls-for-this-step, x%, y%). The parent is
# the printer it is upgraded from, and None marks the one the entry hours buy.
# Order is the order they are drawn in; it plays no part in the costing.
TRACKS = (
    {
        "slug": "bambu",
        "unit": "printers",
        "name": "Bambu Lab",
        "creature": "the hammerhead",
        # Qidi is the only track whose entry hours stop short of its cheapest
        # printer, so every other track opens at zero pearls.
        "entry_pearls": 0,
        "printers": (
            ("A1 Mini", None, 0, 10.68, 60.53),
            ("A1 Mini Combo", "A1 Mini", 240, 23.29, 65.49),
            ("A1", "A1 Mini", 240, 23.55, 54.20),
            ("A1 Combo", "A1", 290, 36.38, 59.12),
            ("P1S", "A1", 290, 36.42, 43.23),
            ("P1S Combo", "P1S", 440, 50.03, 53.33),
            ("P2S", "P1S", 440, 49.24, 39.59),
            ("P2S Combo", "P2S", 290, 62.78, 50.72),
            ("X2D", "P2S", 290, 63.60, 40.00),
            ("X2D Combo", "X2D", 730, 76.52, 53.97),
            ("H2S", "X2D", 1750, 75.45, 34.22),
            ("H2S Combo", "H2S", 730, 89.26, 27.56),
        ),
    },
    {
        "slug": "elegoo",
        "unit": "printers",
        "name": "Elegoo",
        "creature": "the conch",
        "entry_pearls": 0,
        "printers": (
            ("Neptune 4", None, 0, 23.82, 67.64),
            ("Neptune 4 Pro", "Neptune 4", 70, 35.63, 67.27),
            ("Neptune 4 Plus", "Neptune 4 Pro", 400, 49.26, 54.34),
            ("Neptune 4 Max Bundle", "Neptune 4 Plus", 270, 58.58, 39.19),
            ("Centauri Carbon 2", "Neptune 4 Pro", 220, 52.81, 69.10),
            ("Centauri Carbon 2 Combo", "Centauri Carbon 2", 240, 65.02, 62.10),
        ),
    },
    {
        "slug": "qidi",
        "unit": "printers",
        "name": "Qidi",
        "creature": "the humpback",
        # 40 hours is worth $300 on this track and the Q2C is about $495, so
        # the opening printer is hours plus pearls where the others are hours.
        "entry_pearls": 390,
        "printers": (
            ("Q2C", None, 0, 20.60, 20.63),
            ("Q2C Combo", "Q2C", 390, 26.93, 44.83),
            ("Q2", "Q2C", 310, 34.36, 30.01),
            ("Q2 Combo", "Q2", 390, 43.40, 58.98),
            ("Plus 4", "Q2", 390, 46.46, 40.40),
            ("Plus 4 Combo", "Plus 4", 390, 59.09, 65.55),
            ("Plus 5", "Plus 4", 260, 62.60, 39.89),
            ("Plus 5 Combo", "Plus 5", 390, 74.05, 71.17),
            ("Max 4", "Plus 5", 910, 75.91, 43.68),
            ("Max 4 Combo", "Max 4", 520, 86.44, 52.80),
        ),
    },
    {
        "slug": "creality",
        "unit": "printers",
        "name": "Creality",
        "creature": "the coral",
        "entry_pearls": 0,
        "printers": (
            ("Ender 3 V3 SE", None, 0, 16.82, 67.36),
            ("Ender 3 V3 KE", "Ender 3 V3 SE", 160, 26.87, 55.94),
            ("Ender 3 V3 Plus", "Ender 3 V3 KE", 370, 37.92, 36.78),
            ("K2", "Ender 3 V3 KE", 330, 41.26, 55.90),
            ("K2 Combo", "K2", 370, 62.69, 36.09),
            ("K2 Pro", "K2", 520, 65.51, 46.14),
            ("K2 Pro Combo", "K2 Pro", 520, 77.44, 30.41),
            ("K2 Plus", "K2 Pro", 1040, 80.14, 40.64),
            ("K2 Plus Combo", "K2 Plus", 570, 90.58, 23.00),
        ),
    },
    {
        "slug": "byop",
        "unit": "grant tiers",
        "name": "BYOP",
        "creature": "the ray",
        "entry_pearls": 0,
        # A tier is named by the grant it carries, which is the one dollar
        # figure the site shows: it is a budget to spend, not a price to pay.
        # $50 of grant is 105 pearls — the diagrams' "grant cost times 2.1".
        "printers": (
            ("$250 grant", None, 0, 14.77, 33.27),
            ("$300 grant", "$250 grant", 105, 23.48, 24.08),
            ("$350 grant", "$300 grant", 105, 34.09, 33.36),
            ("$400 grant", "$350 grant", 105, 42.17, 42.50),
            ("$450 grant", "$400 grant", 105, 51.18, 37.50),
            ("$500 grant", "$450 grant", 105, 64.96, 38.33),
            ("$550 grant", "$500 grant", 105, 72.90, 45.96),
            ("$600 grant", "$550 grant", 105, 80.97, 36.06),
            ("$650 grant", "$600 grant", 105, 90.16, 53.30),
        ),
    },
)


def _total_pearls(printers, entry_pearls):
    """What each printer costs in pearls all in, keyed by name.

    Walks up the parent chain per printer rather than assuming the tuples are
    in tree order, so the tables above can stay in drawing order.
    """
    steps = {name: (parent, pearls) for name, parent, pearls, _x, _y in printers}
    totals = {}

    for name in steps:
        total, at, seen = entry_pearls, name, set()
        while at is not None:
            if at in seen:
                raise ValueError(f"{name} sits on a cycle of upgrades")
            seen.add(at)
            parent, pearls = steps[at]
            total += pearls
            at = parent
        totals[name] = total

    return totals


def _label(pearls):
    """The one cost a person is shown: hours, and pearls when there are any."""
    if pearls:
        return f"{ENTRY_HOURS} hours + {pearls} pearls"
    return f"{ENTRY_HOURS} hours"


def tracks():
    """The tracks with every printer costed, ready for a template.

    Each printer gains `pearls` (the total, not the step), a `cost` string for
    the hover label, and an `align` telling the overlay which way to hang that
    label so it does not run off the edge of the map.
    """
    out = []

    for track in TRACKS:
        totals = _total_pearls(track["printers"], track["entry_pearls"])
        printers = [
            {
                "name": name,
                "pearls": totals[name],
                "cost": _label(totals[name]),
                "x": x,
                "y": y,
            }
            for name, _parent, _pearls, x, y in track["printers"]
        ]
        out.append({**track, "printers": printers, "entry_cost": _label(track["entry_pearls"])})

    return out


def track(slug):
    """One costed track by slug, or None if there is no such track."""
    return next((t for t in tracks() if t["slug"] == slug), None)
