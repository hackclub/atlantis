# Fonts

`atlantishand.ttf` is the font the site actually uses. It is Patrick Hand
(`patrickhand.ttf`, kept here unmodified) with one change: the lowercase `g` and
its accented variants have a longer, curled tail. Patrick Hand's `g` drops almost
straight down, so at heading sizes "tung" reads as "tunq".

Renamed to "Atlantis Hand" so it is obvious the file is a derivative, not a
stock Patrick Hand build. Both are under the SIL Open Font License (`OFL.txt`);
Patrick Hand declares no Reserved Font Name.

To regenerate after replacing `patrickhand.ttf` (needs `pip install fonttools`):

```python
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates

SWEEP, CURL, POW = 85.0, 165.0, 1.1   # leftward reach, upward curl, easing

font = TTFont("patrickhand.ttf")
glyf = font["glyf"]
for name in ("g", "gcircumflex", "gdotaccent", "gcommaaccent", "gbreve"):
    glyph = glyf[name]
    coords = glyph.getCoordinates(glyf)[0]
    new = []
    for x, y in coords:
        if y < -180:  # the tail, below the bowl
            t = min(max((300 - x) / 300.0, 0.0), 1.0)  # 0 at the stem, 1 at the tip
            x -= SWEEP * t
            y += CURL * (t ** POW)
        new.append((round(x), round(y)))
    glyph.coordinates = GlyphCoordinates(new)
    glyph.program.fromBytecode(b"")  # ttfautohint's hints assume the old outline
    glyph.recalcBounds(glyf)

for nid, value in {
    1: "Atlantis Hand",
    3: "1.003;ATLS;AtlantisHand-Regular",
    4: "Atlantis Hand",
    6: "AtlantisHand-Regular",
    10: "Patrick Hand by Patrick Wagesreiter, with the lowercase g redrawn so its tail reads as a g rather than a q.",
}.items():
    font["name"].setName(value, nid, 3, 1, 0x409)
    font["name"].setName(value, nid, 1, 0, 0)

font.save("atlantishand.ttf")
```

Advance widths are untouched, so swapping the file changes no layout.

`waterlily.ttf` is unused.
