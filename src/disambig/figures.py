"""Shared figure machinery, so that drawing a figure is a rendering job.

Every number in a figure has to come from a computed aggregate, the same way
every number in the post does (CLAUDE.md rule 0). That only holds if building a
figure is boring: if the exporters, the theme, the CSV download and the alt
text are all supplied here, then a figure script is a mapping from an aggregate
table to an SVG and has nowhere to put an invented value. Everything in this
module is deliberately dumb about data.

It also carries the two guards that stand between a figure and the reader:

* ``assert_no_external_references`` refuses a figure document that would fetch
  anything at render time. This is the failure mode CLAUDE.md section 6 is
  designed around. Figures ship as iframes from a static host and are also
  opened as bare files, so one stray CDN ``<script src>`` gives every reader a
  blank rectangle, and it fails silently: the HTML is valid, the deploy
  succeeds, and nothing in the pipeline notices. The guard is blunt on
  purpose: it scans the whole document as lowercase text rather than parsing
  it, because a self-contained figure has no legitimate reason to reach the
  network by any syntax at all.
* ``assert_within_size_budget`` keeps a figure under the 500KB budget, because
  the fix is to aggregate before embedding rather than to ship the raw table.

Colours mirror ``viz/palette.js``, which is the source of truth;
``tests/test_figures.py`` parses that file and fails if this module drifts from
it.
"""

from __future__ import annotations

import csv
import html
import json
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
TEMPLATE_PATH: Final = PROJECT_ROOT / "viz" / "figure_template.html"
FIGURES_DIR: Final = PROJECT_ROOT / "outputs" / "figures"

PROJECT_SLUG: Final = "disambiguation-benchmark"

# The artefact version (CLAUDE.md section 7). Figures live at immutable
# versioned paths, and a revision is a new version directory rather than an
# overwrite, so that a URL framed by a published post never changes content.
# Bumped by hand when the artefact is versioned; nothing here computes it.
#
# DEPLOY CONTRACT for scripts/deploy_figures.py, stated identically in
# src/disambig/figures.py and scripts/figure_selftest.py (tests/test_figures.py
# fails if the two copies drift):
#   deploy_figures.py publishes every directory under outputs/figures/ EXCEPT
#   those whose name starts with an underscore, and it never deletes a
#   previously published version directory. Netlify keeps only the current
#   deploy, so every version directory that any live post frames has to be
#   inside it: deploy the accumulated tree of version directories, never one
#   version on its own, and never prune a version because the post being
#   published does not reference it. Underscore-prefixed directories are local
#   scratch (the toolchain self-test writes to _selftest/), are gitignored as
#   outputs/figures/_*/, and never reach the figure host.
FIGURE_VERSION: Final = "v1.0.0"

FIGURE_ORIGIN: Final = "https://figures.openresearch.wtf"

# CLAUDE.md section 4: under 500KB per figure document.
MAX_FIGURE_BYTES: Final = 500_000

# The iframe height contract, declared once here because the two halves of it
# are written in different languages: viz/figure_template.html posts the
# message and viz/ghost-embed-listener.js receives it, and neither can import
# from the other. tests/test_figures.py asserts both files agree with these.
HEIGHT_MESSAGE_TYPE: Final = "orw-figure-height"
HEIGHT_MESSAGE_KEYS: Final = ("type", "id", "height")

# Mirrors viz/palette.js (Okabe-Ito, colourblind-safe); that file is the source
# of truth and is byte-for-byte the palette used by the sibling projects.
THEME_KEYS: Final = (
    "background",
    "text",
    "muted",
    "grid",
    "accent",
    "accent2",
    "accent3",
    "accent4",
)
THEMES: Final[dict[str, dict[str, str]]] = {
    "light": {
        "background": "#ffffff",
        "text": "#1a1a1a",
        "muted": "#6b6b6b",
        "grid": "#e0e0e0",
        "accent": "#0072B2",
        "accent2": "#E69F00",
        "accent3": "#009E73",
        "accent4": "#CC79A7",
    },
    "dark": {
        "background": "#111418",
        "text": "#f0f0f0",
        "muted": "#9a9a9a",
        "grid": "#33383f",
        "accent": "#56B4E9",
        "accent2": "#E69F00",
        "accent3": "#009E73",
        "accent4": "#CC79A7",
    },
}

# Categorical order, colour paired with a marker shape. Colour never carries a
# category on its own, so a series takes both from the same entry.
CATEGORICAL: Final = (
    ("accent", "circle"),
    ("accent2", "square"),
    ("accent3", "triangle"),
    ("accent4", "diamond"),
)

# The only two numbers in the scale derivations, named on both sides because
# they are the only place the JS and Python implementations could disagree.
SEQUENTIAL_FLOOR: Final = 0.15
DIVERGING_MID_MIX: Final = 0.12

FONT_STACK: Final = "system-ui, -apple-system, Segoe UI, Helvetica, Arial, sans-serif"

# The template carries the same values as CSS variables between these markers,
# and render_figure_html regenerates the whole block, so the checked-in template
# is a working bare file and the rendered figure can still never disagree with
# the palette.
_THEME_CSS_BANNER: Final = (
    "ORW-THEME-CSS-BEGIN generated from viz/palette.js by src/disambig/figures.py"
)

# SVG header geometry, so a set of figures has the same title block.
TITLE_Y: Final = 34
SUBTITLE_Y: Final = 56
SUBTITLE_LINE_HEIGHT: Final = 18


class ExternalReferenceError(RuntimeError):
    """A figure document refers to something it would have to fetch."""


class FigureSizeError(RuntimeError):
    """A figure document is over the size budget."""


class TemplateError(RuntimeError):
    """The figure template could not be rendered as written."""


def require_theme(theme_name: str) -> dict[str, str]:
    theme = THEMES.get(theme_name)
    if theme is None:
        raise KeyError(f"Unknown theme {theme_name!r}; known themes: {sorted(THEMES)}")
    return theme


def _parse_hex(colour: str) -> tuple[int, int, int]:
    value = colour.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected a 6-digit hex colour, got {colour!r}")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def mix_hex(start: str, end: str, position: float) -> str:
    """Channel-wise mix in 8-bit sRGB, rounding half up.

    Not perceptually uniform, and deliberately the same simple arithmetic as
    ``mix`` in viz/palette.js, including the rounding: ``int(x + 0.5)`` matches
    JavaScript's ``Math.round`` where Python's ``round`` would not, because
    Python rounds halves to even. Output is lowercase hex, so a mixed colour is
    distinguishable from a palette literal at a glance.
    """
    from_rgb = _parse_hex(start)
    to_rgb = _parse_hex(end)
    channels = [int(a + (b - a) * position + 0.5) for a, b in zip(from_rgb, to_rgb, strict=True)]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def sequential_scale(theme_name: str, steps: int) -> list[str]:
    """Background towards the theme accent: one ordered quantity, not categories."""
    theme = require_theme(theme_name)
    if steps < 1:
        raise ValueError("sequential_scale needs at least one step")
    if steps == 1:
        return [theme["accent"]]
    return [
        mix_hex(
            theme["background"],
            theme["accent"],
            SEQUENTIAL_FLOOR + (1 - SEQUENTIAL_FLOOR) * (index / (steps - 1)),
        )
        for index in range(steps)
    ]


def diverging_scale(theme_name: str, steps: int) -> list[str]:
    """Okabe-Ito orange to a near-neutral midpoint to the theme accent.

    Orange against blue survives all three common forms of colour blindness,
    which is why the diverging pair is not the usual red and green.
    """
    theme = require_theme(theme_name)
    if steps < 2:
        raise ValueError("diverging_scale needs at least two steps")
    mid = mix_hex(theme["background"], theme["text"], DIVERGING_MID_MIX)
    scale: list[str] = []
    for index in range(steps):
        position = index / (steps - 1)
        if position <= 0.5:
            scale.append(mix_hex(theme["accent2"], mid, position * 2))
        else:
            scale.append(mix_hex(mid, theme["accent"], (position - 0.5) * 2))
    return scale


def theme_css(indent: str = "  ") -> str:
    """The template's theme block, generated from THEMES so it cannot drift.

    Light values sit on bare ``:root`` and dark values override them under
    ``prefers-color-scheme``, because a framed figure cannot see the parent's
    theme and never gets the chance to ask.
    """
    lines = [f"{indent}/* {_THEME_CSS_BANNER} */"]
    lines.append(f"{indent}:root {{")
    for key in THEME_KEYS:
        lines.append(f"{indent}  --orw-{key}: {THEMES['light'][key]};")
    lines.append(f"{indent}}}")
    lines.append(f"{indent}@media (prefers-color-scheme: dark) {{")
    lines.append(f"{indent}  :root {{")
    for key in THEME_KEYS:
        lines.append(f"{indent}    --orw-{key}: {THEMES['dark'][key]};")
    lines.append(f"{indent}  }}")
    lines.append(f"{indent}}}")
    lines.append(f"{indent}/* ORW-THEME-CSS-END */")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# SVG scaffolding
# --------------------------------------------------------------------------


def svg_text(
    x: float,
    y: float,
    text: str,
    *,
    size: float,
    fill: str,
    weight: str | None = None,
    anchor: str | None = None,
    letter_spacing: str | None = None,
) -> str:
    """A ``<text>`` element with the label escaped.

    Escaping here rather than at the call site because affiliation strings and
    institution names carry ampersands, angle brackets and mojibake as a matter
    of routine, and one unescaped one breaks the whole document.
    """
    attrs = [f'x="{_num(x)}"', f'y="{_num(y)}"', f'font-size="{_num(size)}"', f'fill="{fill}"']
    if weight is not None:
        attrs.append(f'font-weight="{weight}"')
    if anchor is not None:
        attrs.append(f'text-anchor="{anchor}"')
    if letter_spacing is not None:
        attrs.append(f'letter-spacing="{letter_spacing}"')
    return f"<text {' '.join(attrs)}>{html.escape(text)}</text>"


def svg_rect(
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    fill: str,
    rx: float | None = None,
    opacity: float | None = None,
    stroke: str | None = None,
    stroke_width: float | None = None,
) -> str:
    attrs = [
        f'x="{_num(x)}"',
        f'y="{_num(y)}"',
        f'width="{_num(width)}"',
        f'height="{_num(height)}"',
        f'fill="{fill}"',
    ]
    if rx is not None:
        attrs.append(f'rx="{_num(rx)}"')
    if opacity is not None:
        attrs.append(f'opacity="{_num(opacity)}"')
    if stroke is not None:
        attrs.append(f'stroke="{stroke}"')
    if stroke_width is not None:
        attrs.append(f'stroke-width="{_num(stroke_width)}"')
    return f"<rect {' '.join(attrs)}/>"


def svg_line(
    x1: float, y1: float, x2: float, y2: float, *, stroke: str, stroke_width: float = 1
) -> str:
    return (
        f'<line x1="{_num(x1)}" y1="{_num(y1)}" x2="{_num(x2)}" y2="{_num(y2)}" '
        f'stroke="{stroke}" stroke-width="{_num(stroke_width)}"/>'
    )


def marker_shape(shape: str, cx: float, cy: float, size: float, *, fill: str) -> str:
    """A categorical marker: circle, square, triangle or diamond.

    Paired with CATEGORICAL so that a series never depends on colour alone
    (CLAUDE.md section 4). ``size`` is the half-extent, so shapes of one size
    read as the same weight.
    """
    if shape == "circle":
        return f'<circle cx="{_num(cx)}" cy="{_num(cy)}" r="{_num(size)}" fill="{fill}"/>'
    if shape == "square":
        return svg_rect(cx - size, cy - size, size * 2, size * 2, fill=fill)
    if shape == "triangle":
        points = (
            f"{_num(cx)},{_num(cy - size)} {_num(cx + size)},{_num(cy + size)} "
            f"{_num(cx - size)},{_num(cy + size)}"
        )
        return f'<polygon points="{points}" fill="{fill}"/>'
    if shape == "diamond":
        points = (
            f"{_num(cx)},{_num(cy - size)} {_num(cx + size)},{_num(cy)} "
            f"{_num(cx)},{_num(cy + size)} {_num(cx - size)},{_num(cy)}"
        )
        return f'<polygon points="{points}" fill="{fill}"/>'
    raise ValueError(
        f"Unknown marker shape {shape!r}; known shapes: circle, square, triangle, diamond"
    )


def header_height(subtitle_lines: Sequence[str]) -> int:
    """Pixels the title block occupies, so callers can offset the plot area."""
    if not subtitle_lines:
        return TITLE_Y + 14
    return SUBTITLE_Y + SUBTITLE_LINE_HEIGHT * (len(subtitle_lines) - 1) + 16


def svg_document(
    *,
    width: float,
    height: float,
    theme_name: str,
    body: str,
    title: str,
    subtitle_lines: Sequence[str] = (),
) -> str:
    """A complete standalone SVG: painted background, title block, then body.

    The background is painted rather than left transparent because these SVGs
    are also converted to PNGs for the email fallback, where a transparent
    background renders as whatever the mail client feels like.
    """
    theme = require_theme(theme_name)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_num(width)} {_num(height)}" '
        f'width="{_num(width)}" height="{_num(height)}" font-family="{FONT_STACK}">',
        svg_rect(0, 0, width, height, fill=theme["background"]),
        svg_text(16, TITLE_Y, title, size=20, fill=theme["text"], weight="700"),
    ]
    for index, line in enumerate(subtitle_lines):
        parts.append(
            svg_text(
                16,
                SUBTITLE_Y + index * SUBTITLE_LINE_HEIGHT,
                line,
                size=14,
                fill=theme["muted"],
            )
        )
    parts.append(body)
    parts.append("</svg>")
    return "\n".join(parts)


def _num(value: float) -> str:
    """Compact number formatting: integers stay integers, floats keep 1 decimal.

    Keeps the SVG small and, more usefully, keeps it diffable between runs.
    """
    if isinstance(value, int) or value == int(value):
        return str(int(value))
    return f"{value:.1f}"


# --------------------------------------------------------------------------
# Figure paths and the Ghost embed card
# --------------------------------------------------------------------------


def figure_id(name: str) -> str:
    """The immutable versioned identity of a figure, e.g. ``slug/v1.0.0/name``.

    Doubles as the id in the iframe height message, which is why it is also the
    path under the figure host: the listener confirms one against the other.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError(
            f"Figure name {name!r} must be lowercase letters, digits and hyphens: it becomes a "
            "URL path segment and a JavaScript string literal."
        )
    return f"{PROJECT_SLUG}/{FIGURE_VERSION}/{name}"


def figure_url(name: str, suffix: str) -> str:
    """Absolute URL of one figure artefact on the figure host."""
    return f"{FIGURE_ORIGIN}/{figure_id(name)}.{suffix.lstrip('.')}"


def ghost_embed_card(
    name: str,
    *,
    iframe_title: str,
    alt_text: str,
    caption: str,
    fallback_height_px: int = 520,
) -> str:
    """One Ghost HTML card for one figure (CLAUDE.md section 6.3).

    Two verified facts about Ghost shape this, and both cut against the obvious
    simplification:

    * Ghost does not strip scripts or iframes from newsletters. The HTML card is
      emitted verbatim to every target, email included, and the Content API and
      RSS carry it verbatim too. Embeds fail in email because email clients
      refuse to run them, not because Ghost removed them. So the static fallback
      image has to stay in the markup and be hidden on web by site CSS, which is
      what the ``orw-figure-fallback`` class is for.
    * Per-card email and web visibility is not an option here. It is
      Lexical-only, unavailable through ``?source=html``, and the hidden side
      renders empty, which would strip the fallback out of what RSS serves.

    ``fallback_height_px`` is the height the iframe shows until the site-footer
    listener sizes it, not a hard-coded figure height.
    """
    identity = figure_id(name)
    return (
        "<!--kg-card-begin: html-->\n"
        f'<figure class="orw-figure" data-figure="{identity}">\n'
        "  <iframe\n"
        f'    src="{FIGURE_ORIGIN}/{identity}.html"\n'
        f'    title="{html.escape(iframe_title, quote=True)}"\n'
        '    loading="lazy" scrolling="no" '
        f'style="width:100%;border:0;height:{fallback_height_px}px"></iframe>\n'
        '  <img class="orw-figure-fallback"\n'
        f'       src="{FIGURE_ORIGIN}/{identity}.png"\n'
        f'       alt="{html.escape(alt_text, quote=True)}">\n'
        "  <figcaption>\n"
        f"    {html.escape(caption)}\n"
        f'    <a href="{FIGURE_ORIGIN}/{identity}.html">Interactive version</a> &middot;\n'
        f'    <a href="{FIGURE_ORIGIN}/{identity}.csv">Get the data</a>\n'
        "  </figcaption>\n"
        "</figure>\n"
        "<!--kg-card-end: html-->"
    )


# --------------------------------------------------------------------------
# Rendering the self-contained figure document
# --------------------------------------------------------------------------


def _region_bounds(text: str, begin_marker: str, end_marker: str) -> tuple[int, int]:
    begin = text.find(begin_marker)
    if begin == -1:
        raise TemplateError(f"Template is missing the marker {begin_marker!r}")
    end = text.find(end_marker, begin)
    if end == -1:
        raise TemplateError(f"Template is missing the marker {end_marker!r} after {begin_marker!r}")
    line_start = text.rfind("\n", 0, begin) + 1
    line_end = text.find("\n", end + len(end_marker))
    if line_end == -1:
        line_end = len(text)
    return line_start, line_end + 1


def _replace_region(text: str, begin_marker: str, end_marker: str, replacement: str) -> str:
    start, stop = _region_bounds(text, begin_marker, end_marker)
    body = f"{replacement}\n" if replacement else ""
    return text[:start] + body + text[stop:]


def render_figure_html(
    *,
    name: str,
    title: str,
    alt_text: str,
    svg_light: str,
    svg_dark: str,
    data_href: str,
    data_label: str = "Get the data (CSV)",
    inline_data: object | None = None,
    inline_script: str | None = None,
    template_path: Path = TEMPLATE_PATH,
) -> str:
    """Render one self-contained figure document from the template.

    Fails rather than emitting a document that is subtly wrong: an unsubstituted
    placeholder, a missing template marker, or any external reference is an
    exception, not a warning. A figure that silently loses its script tag looks
    fine in the deploy log and blank on the page.
    """
    identity = figure_id(name)
    document = template_path.read_text(encoding="utf-8")
    # The template's own notes are for whoever opens the template, not for the
    # reader of a figure, and they would otherwise ship in every document.
    document = _replace_region(
        document, "<!-- ORW-TEMPLATE-NOTES-BEGIN", "ORW-TEMPLATE-NOTES-END -->", ""
    )
    document = _replace_region(
        document, "/* ORW-THEME-CSS-BEGIN", "/* ORW-THEME-CSS-END */", theme_css()
    )

    if inline_data is None:
        document = _replace_region(document, "<!-- ORW-DATA-BEGIN", "<!-- ORW-DATA-END -->", "")
        data_json = ""
    else:
        # "<" escaped so that a string in the data can never close the script
        # element that carries it.
        data_json = json.dumps(inline_data, ensure_ascii=False, sort_keys=True).replace(
            "<", "\\u003c"
        )

    if inline_script is None:
        document = _replace_region(document, "<!-- ORW-SCRIPT-BEGIN", "<!-- ORW-SCRIPT-END -->", "")
    elif "</script" in inline_script.lower():
        raise TemplateError("Inline script contains a closing script tag and would break the page")

    substitutions = {
        "ORW_TITLE": html.escape(title),
        "ORW_ALT_TEXT": html.escape(alt_text, quote=True),
        "ORW_FIGURE_ID": identity,
        "ORW_SVG_LIGHT": svg_light,
        "ORW_SVG_DARK": svg_dark,
        "ORW_DATA_HREF": html.escape(data_href, quote=True),
        "ORW_DATA_LABEL": html.escape(data_label),
        "ORW_INLINE_DATA": data_json,
        "ORW_SCRIPT": inline_script or "",
    }
    for key, value in substitutions.items():
        document = document.replace(f"{{{{{key}}}}}", value)

    leftover = re.search(r"\{\{([A-Za-z0-9_]+)\}\}", document)
    if leftover is not None:
        raise TemplateError(
            f"Figure {name}: template placeholder {{{{{leftover.group(1)}}}}} was never "
            "substituted. Add it to render_figure_html or remove it from the template."
        )
    assert_no_external_references(document, label=f"{name}.html")
    return document


# --------------------------------------------------------------------------
# The two guards
# --------------------------------------------------------------------------

# Namespace URIs are identifiers, not fetches: a browser never requests them.
# They are the only http URLs allowed in a figure document, and they are matched
# with their surrounding quotes so that a real URL sharing the prefix cannot
# hide behind one. Lowercase, like everything else the scan reads.
_ALLOWED_URI_LITERALS: Final = (
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/1999/xlink",
)

# A forward slash written as a backslash escape, which JS and JSON string
# literals permit: "https:\/\/cdn" is the same URL to the browser.
_ESCAPED_SLASH: Final = re.compile(r"\\(?:/|u002f|x2f)")

# "//" where a URL can start: after an attribute's equals sign (quoted or not),
# after any kind of quote (a JS or CSS string) or after an opening parenthesis
# (url(//...)). A JS line comment after whitespace matches none of these.
_PROTOCOL_RELATIVE: Final = ("=//", '"//', "'//", "`//", "(//")

# An attribute value: double quoted, single quoted, or not quoted at all.
_ATTR_VALUE: Final = r"""(?:"([^"]*)"|'([^']*)'|([^\s>"']+))"""
_SRC_ATTR: Final = re.compile(r"\b(?:src|srcset)\s*=\s*" + _ATTR_VALUE)
# href is a fetch on the elements that load what it points at: a stylesheet
# link, an SVG image, an SVG use (with or without the xlink prefix). An <a href>
# is navigation, and the "get the data" link depends on it staying allowed.
_LOADING_HREF: Final = re.compile(
    r"<(?:link|image|use)\b[^>]*?\b(?:xlink:)?href\s*=\s*" + _ATTR_VALUE
)
_CSS_URL: Final = re.compile(r"""url\(\s*(["']?)([^)"']*)\1\s*\)""")
_CSS_IMAGE_SET: Final = re.compile(r"image-set\(([^)]*)\)")
_QUOTED: Final = re.compile(r"""["']([^"']*)["']""")

# Ways a script reaches the network, or sets up an element that will. Matched
# blind: a self-contained figure has no reason to do any of these, so a call
# that might have resolved to inlined content is refused rather than analysed.
_SCRIPT_TRIPWIRES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "setAttribute of src or href",
        re.compile(r"""\bsetattribute\s*\(\s*["'`](?:xlink:)?(?:src|srcset|href)["'`]"""),
    ),
    ("dynamic import()", re.compile(r"\bimport\s*\(")),
    ("fetch()", re.compile(r"\bfetch\s*\(")),
    ("XMLHttpRequest", re.compile(r"\bxmlhttprequest\b")),
    ("WebSocket", re.compile(r"\bwebsocket\b")),
    ("navigator.sendBeacon", re.compile(r"\bsendbeacon\b")),
    ("EventSource", re.compile(r"\beventsource\b")),
    ("new URL()", re.compile(r"\bnew\s+url\s*\(")),
    # A static ES-module import of a sibling file is the realistic pasted-D3
    # case, and it is not self-contained either.
    ("static import", re.compile(r"""\bimport\s+(?:[\w*{}\s,$]+\s+from\s+)?["'`]""")),
    (
        "bracket assignment of src or href",
        re.compile(r"""\[\s*["'`](?:xlink:)?(?:src|srcset|href)["'`]\s*\]\s*="""),
    ),
    ("Object.assign()", re.compile(r"\bobject\.assign\s*\(")),
    ("<object> or <embed>", re.compile(r"<(?:object|embed)\b")),
    ("<feImage>", re.compile(r"<feimage\b")),
    ("meta refresh", re.compile(r"""http-equiv\s*=\s*["']?\s*refresh""")),
)

# What a src, a loading href, a CSS url() or an image-set() entry may start
# with and still fetch nothing: a fragment of this document, or inlined bytes.
_INLINE_ONLY: Final = ("#", "data:")


def _snippet(text: str, index: int) -> str:
    window = text[max(0, index - 30) : index + 70]
    return " ".join(window.split())


def _scan_text(document: str) -> str:
    """The document as the guard reads it.

    Lowercased, so no check depends on the case of a scheme, a tag or a CSS
    at-rule; HTML entities resolved, so a slash spelled ``&#x2f;`` is a slash;
    backslash-escaped slashes resolved, so a JS string literal cannot hide a
    URL; and the allowed namespace literals blanked out, quotes kept, so the
    scheme scan does not trip over them.
    """
    text = _ESCAPED_SLASH.sub("/", html.unescape(document.lower()))
    for literal in _ALLOWED_URI_LITERALS:
        for quote in ('"', "'"):
            text = text.replace(f"{quote}{literal}{quote}", f"{quote}{quote}")
    return text


def _attr_value(match: re.Match[str]) -> str:
    """The value captured by whichever _ATTR_VALUE alternative matched."""
    for group in match.groups():
        if group is not None:
            return group
    raise AssertionError("_ATTR_VALUE matched without capturing a value")


def _find_all(text: str, needle: str) -> Iterable[int]:
    start = text.find(needle)
    while start != -1:
        yield start
        start = text.find(needle, start + 1)


def assert_no_external_references(document: str, *, label: str) -> None:
    """Refuse a figure document that would fetch anything at render time.

    Blunt by design. A self-contained figure has no legitimate reason to reach
    the network at all, so the whole document is scanned as one lowercase
    string (see ``_scan_text``) and anything that looks like a fetch is an
    offence, whatever syntax surrounds it: markup, CSS or script. The checks:

    * no ``http://`` or ``https://`` anywhere, other than the allowed XML
      namespace literals (SVG, XLink, XHTML), which browsers never request;
    * no protocol-relative ``//`` after an attribute's ``=``, after a quote of
      any kind, or after ``(``;
    * no CSS ``@import`` in any letter case;
    * every ``src`` and ``srcset``, quoted or not, is a ``data:`` URI;
    * every ``href`` on ``<link>``, ``<image>`` and ``<use>``, quoted or not,
      ``xlink:`` or not, is a fragment or a ``data:`` URI;
    * every CSS ``url()`` and every entry of ``image-set()`` is a fragment or a
      ``data:`` URI;
    * no script reaches for ``setAttribute`` with ``src`` or ``href``, dynamic
      ``import()``, ``fetch()``, ``XMLHttpRequest``, ``WebSocket``,
      ``navigator.sendBeacon``, ``EventSource`` or ``new URL()``; nor a static
      ES-module ``import ... from``, a bracket assignment of ``src``/``href``,
      or ``Object.assign()``;
    * no ``<object>``, ``<embed>``, ``<feImage>`` or ``<meta http-equiv=refresh>``,
      each of which loads or navigates to something.

    Relative references are caught too, because a figure with a sibling file is
    not self-contained either. The bluntness has a cost worth knowing: an
    identifier that happens to be a URL, a ROR id or a DOI link inlined as
    data, trips the first check, so inline the bare identifier instead. It is
    a tripwire on the ways a figure gets broken by pasted boilerplate, not a
    sandbox against a determined author, and it does not parse HTML.
    """
    text = _scan_text(document)
    offences: list[str] = []

    for scheme in ("http://", "https://"):
        for start in _find_all(text, scheme):
            offences.append(f"external URL: {_snippet(text, start)}")
    for marker in _PROTOCOL_RELATIVE:
        for start in _find_all(text, marker):
            offences.append(f"protocol-relative URL: {_snippet(text, start)}")
    for start in _find_all(text, "@import"):
        offences.append(f"CSS @import: {_snippet(text, start)}")
    for match in _SRC_ATTR.finditer(text):
        if not _attr_value(match).startswith("data:"):
            offences.append(f"loaded src: {_snippet(text, match.start())}")
    for match in _LOADING_HREF.finditer(text):
        if not _attr_value(match).startswith(_INLINE_ONLY):
            offences.append(f"loading href: {_snippet(text, match.start())}")
    for match in _CSS_URL.finditer(text):
        if not match.group(2).startswith(_INLINE_ONLY):
            offences.append(f"CSS url(): {_snippet(text, match.start())}")
    for match in _CSS_IMAGE_SET.finditer(text):
        for quoted in _QUOTED.finditer(match.group(1)):
            if not quoted.group(1).startswith(_INLINE_ONLY):
                offences.append(f"CSS image-set(): {_snippet(text, match.start())}")
    for description, pattern in _SCRIPT_TRIPWIRES:
        for match in pattern.finditer(text):
            offences.append(f"script {description}: {_snippet(text, match.start())}")

    if offences:
        listed = "\n  ".join(dict.fromkeys(offences))
        raise ExternalReferenceError(
            f"{label} refers to something it would have to fetch, so it would render blank "
            f"for readers who cannot reach it. Inline it instead.\n  {listed}"
        )


def assert_within_size_budget(path: Path, *, budget_bytes: int = MAX_FIGURE_BYTES) -> int:
    """Fail if a figure document is over budget. Returns its size in bytes."""
    size = path.stat().st_size
    if size > budget_bytes:
        raise FigureSizeError(
            f"{path.name} is {size} bytes, over the {budget_bytes} byte figure budget. "
            "Aggregate the data before embedding it rather than shipping the whole table."
        )
    return size


# --------------------------------------------------------------------------
# Sidecars every figure ships with: CSV, alt text, PNG
#
# Provenance is not here: figures and tables record it the same way, so it
# lives in disambig.provenance and a figure script imports it from there.
# --------------------------------------------------------------------------


def write_figure_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[object]]) -> int:
    """Write the aggregate behind one figure. Returns the number of data rows.

    This is the file the reader gets from the "get the data" link, so it is the
    figure's own claim about what it is drawing. A row whose width does not
    match the header is a bug in the calling script and crashes here rather
    than quietly shifting a column.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            if len(row) != len(header):
                raise ValueError(
                    f"{path.name}: row {count} has {len(row)} values for {len(header)} columns"
                )
            writer.writerow(row)
            count += 1
    return count


def read_alt_text(path: Path) -> dict[str, str]:
    """Read a figures directory's alt_text.json, or an empty map if absent."""
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: top level must be an object mapping figure name to alt text")
    return {str(key): str(value) for key, value in loaded.items()}


def upsert_alt_text(path: Path, name: str, alt: str) -> None:
    """Record the alt text for one figure, merging into any existing entries.

    Requires ``<name>.html`` to sit beside the alt text file, so the map cannot
    accumulate entries for figures that do not exist. An alt text string with no
    figure is worse than a missing one: it reads as evidence a figure was built.
    """
    figure_doc = path.parent / f"{name}.html"
    if not figure_doc.exists():
        raise FileNotFoundError(
            f"Refusing to record alt text for {name!r}: {figure_doc} does not exist. "
            "Write the figure document first."
        )
    if not alt.strip():
        raise ValueError(f"Alt text for {name!r} is empty")
    entries = read_alt_text(path)
    entries[name] = alt
    path.write_text(
        json.dumps(dict(sorted(entries.items())), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def find_rsvg_convert() -> str:
    """Locate rsvg-convert, or say exactly what is missing.

    Homebrew's bin is not always on PATH in a non-login shell, so the usual
    install location is tried before giving up.
    """
    found = shutil.which("rsvg-convert")
    if found is not None:
        return found
    homebrew = Path("/opt/homebrew/bin/rsvg-convert")
    if homebrew.exists():
        return str(homebrew)
    raise FileNotFoundError(
        "rsvg-convert not found on PATH or at /opt/homebrew/bin/rsvg-convert. "
        "Install it with `brew install librsvg`; the PNG fallbacks cannot be built without it."
    )


def export_png(svg_path: Path, png_path: Path, *, zoom: int = 2) -> None:
    """Rasterise one SVG at 2x for slides and the email fallback.

    ``check=True``: a failed conversion means the email fallback is missing or
    stale, which is exactly the failure a reader on email would see and nobody
    else would.
    """
    subprocess.run(
        [find_rsvg_convert(), "--zoom", str(zoom), "--output", str(png_path), str(svg_path)],
        check=True,
    )


def height_message_contract() -> Mapping[str, object]:
    """The height message as both halves of the contract have to see it.

    Returned rather than duplicated in the tests so there is one place to look
    when the template and the listener disagree.
    """
    return {"type": HEIGHT_MESSAGE_TYPE, "keys": HEIGHT_MESSAGE_KEYS, "origin": FIGURE_ORIGIN}
