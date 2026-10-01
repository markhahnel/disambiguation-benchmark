"""Figure toolchain: the guards, the palette mirror, and the height contract.

Three of these tests exist to catch mistakes that are invisible in the output.
A CDN reference renders blank only for the reader, a colour drift shows up only
in the PNG fallback, and the height message is a contract between a Python
template and a hand-pasted piece of JavaScript that cannot import from each
other. All three are asserted here or nowhere.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from disambig.figures import (
    CATEGORICAL,
    DIVERGING_MID_MIX,
    FIGURE_ORIGIN,
    HEIGHT_MESSAGE_KEYS,
    HEIGHT_MESSAGE_TYPE,
    MAX_FIGURE_BYTES,
    SEQUENTIAL_FLOOR,
    TEMPLATE_PATH,
    THEME_KEYS,
    THEMES,
    ExternalReferenceError,
    FigureSizeError,
    TemplateError,
    assert_no_external_references,
    assert_within_size_budget,
    diverging_scale,
    figure_id,
    figure_url,
    ghost_embed_card,
    height_message_contract,
    mix_hex,
    read_alt_text,
    render_figure_html,
    sequential_scale,
    theme_css,
    upsert_alt_text,
    write_figure_csv,
)

REPO = Path(__file__).resolve().parents[1]
PALETTE_JS = REPO / "viz" / "palette.js"
LISTENER_JS = REPO / "viz" / "ghost-embed-listener.js"

SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'
MINIMAL_SVG = f'<svg {SVG_NS} viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'


# ---------------------------------------------------------------------------
# viz/palette.js is the source of truth; nothing may drift from it
# ---------------------------------------------------------------------------


def _js_literal(source: str, name: str) -> object:
    """Read one ``export const <name> = <literal>;`` out of a JS file.

    Deliberately a small tolerant reader rather than a JS parser: it strips line
    comments, quotes bare keys and drops trailing commas, then hands the result
    to json.loads. If palette.js ever grows something this cannot read, the test
    fails loudly, which is the correct outcome for a file two other languages
    mirror by hand.
    """
    marker = f"const {name} = "
    start = source.index(marker) + len(marker)
    depth = 0
    in_string = False
    for offset, char in enumerate(source[start:]):
        if in_string:
            in_string = char != '"'
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                literal = source[start : start + offset + 1]
                break
    else:
        raise AssertionError(f"Unbalanced literal for {name} in {PALETTE_JS}")

    no_comments = "\n".join(line.split("//")[0] for line in literal.splitlines())
    no_commas = re.sub(r",(\s*[}\]])", r"\1", no_comments)
    quoted_keys = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)", r'\1"\2"\3', no_commas)
    return json.loads(quoted_keys)


def test_python_themes_match_palette_js() -> None:
    parsed = _js_literal(PALETTE_JS.read_text(encoding="utf-8"), "palette")
    assert parsed == THEMES


def test_categorical_pairs_match_palette_js() -> None:
    parsed = _js_literal(PALETTE_JS.read_text(encoding="utf-8"), "categorical")
    assert parsed == [{"key": key, "shape": shape} for key, shape in CATEGORICAL]


def test_scale_parameters_match_palette_js() -> None:
    # The only two numbers in the derivation, so the only place the JS and
    # Python scale implementations could disagree numerically.
    source = PALETTE_JS.read_text(encoding="utf-8")
    assert f"const SEQUENTIAL_FLOOR = {SEQUENTIAL_FLOOR};" in source
    assert f"const DIVERGING_MID_MIX = {DIVERGING_MID_MIX};" in source


def test_template_theme_block_is_generated_from_the_palette() -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    end_marker = "/* ORW-THEME-CSS-END */"
    start = template.index("  /* ORW-THEME-CSS-BEGIN")
    stop = template.index(end_marker) + len(end_marker)
    assert template[start:stop] == theme_css()


def test_theme_css_declares_every_palette_key_in_both_themes() -> None:
    css = theme_css()
    for key in THEME_KEYS:
        assert f"--orw-{key}: {THEMES['light'][key]};" in css
        assert f"--orw-{key}: {THEMES['dark'][key]};" in css


# ---------------------------------------------------------------------------
# Scales: golden values computed by hand
# ---------------------------------------------------------------------------


def test_mix_rounds_half_up_like_javascript() -> None:
    # 0 + 1*0.5 = 0.5 per channel. Math.round gives 1; Python's round() would
    # give 0, which is why mix_hex uses int(x + 0.5).
    assert mix_hex("#000000", "#010101", 0.5) == "#010101"


def test_mix_endpoints_are_exact() -> None:
    assert mix_hex("#ffffff", "#0072B2", 0.0) == "#ffffff"
    assert mix_hex("#ffffff", "#0072B2", 1.0) == "#0072b2"


def test_sequential_light_golden() -> None:
    # floor 0.15, three steps, so positions 0.15, 0.575, 1.0 from #ffffff to
    # #0072B2. Channel 0: 255 - 255*0.15 = 216.75 -> 217 = 0xd9;
    # 255 - 255*0.575 = 108.375 -> 108 = 0x6c; then the accent itself.
    assert sequential_scale("light", 3) == ["#d9eaf3", "#6caed3", "#0072b2"]


def test_sequential_dark_starts_from_the_dark_background() -> None:
    scale = sequential_scale("dark", 4)
    assert scale[-1] == THEMES["dark"]["accent"].lower()
    # First stop is 15% of the way from #111418 to #56B4E9:
    #   17 + (86-17)*0.15  = 27.35 -> 27 = 0x1b
    #   20 + (180-20)*0.15 = 44.0  -> 44 = 0x2c
    #   24 + (233-24)*0.15 = 55.35 -> 55 = 0x37
    assert scale[0] == "#1b2c37"


def test_sequential_single_step_is_the_accent() -> None:
    assert sequential_scale("light", 1) == [THEMES["light"]["accent"]]


def test_diverging_light_golden() -> None:
    # Midpoint is #ffffff mixed 12% towards #1a1a1a: 255 - 229*0.12 = 227.52
    # -> 228 = 0xe4. Ends are the Okabe-Ito orange and the theme accent; the
    # quarter points are each half way to the midpoint.
    assert diverging_scale("light", 5) == [
        "#e69f00",
        "#e5c272",
        "#e4e4e4",
        "#72abcb",
        "#0072b2",
    ]


def test_scale_argument_errors_are_loud() -> None:
    with pytest.raises(ValueError):
        sequential_scale("light", 0)
    with pytest.raises(ValueError):
        diverging_scale("light", 1)
    with pytest.raises(KeyError):
        sequential_scale("sepia", 3)


# ---------------------------------------------------------------------------
# Guard 1: no external references
# ---------------------------------------------------------------------------


def test_guard_catches_a_cdn_script() -> None:
    document = (
        "<!doctype html><html><body>"
        '<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>'
        "</body></html>"
    )
    with pytest.raises(ExternalReferenceError) as excinfo:
        assert_no_external_references(document, label="collision-scatter.html")
    message = str(excinfo.value)
    assert "collision-scatter.html" in message
    assert "cdn.jsdelivr.net" in message


def test_guard_catches_a_cdn_stylesheet_and_a_web_font() -> None:
    document = (
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
        "<style>@import url(https://example.invalid/base.css);</style>"
    )
    with pytest.raises(ExternalReferenceError) as excinfo:
        assert_no_external_references(document, label="figure.html")
    message = str(excinfo.value)
    assert "fonts.googleapis.com" in message
    assert "@import" in message


def test_guard_catches_protocol_relative_and_sibling_files() -> None:
    with pytest.raises(ExternalReferenceError):
        assert_no_external_references('<script src="//cdn.example/d3.js"></script>', label="f")
    # A relative script is not self-contained either: the figure is also opened
    # as a bare file, and one file is all it gets.
    with pytest.raises(ExternalReferenceError):
        assert_no_external_references('<script src="d3.min.js"></script>', label="f")
    with pytest.raises(ExternalReferenceError):
        assert_no_external_references("<style>body{background:url(bg.png)}</style>", label="f")


def test_guard_allows_namespace_uris_data_uris_and_relative_anchors() -> None:
    document = (
        f"<!doctype html><html><body>{MINIMAL_SVG}"
        '<a href="figure.csv" download>Get the data</a>'
        '<script src="data:text/javascript,void%200"></script>'
        "<style>body{background:url(data:image/gif;base64,R0lGOD)}"
        ".m{clip-path:url(#mask)}</style>"
        "</body></html>"
    )
    assert_no_external_references(document, label="ok.html")


def test_guard_does_not_let_a_url_hide_behind_an_allowed_namespace() -> None:
    with pytest.raises(ExternalReferenceError):
        assert_no_external_references(
            '<script src="http://www.w3.org/2000/svg/../../evil.js"></script>', label="f"
        )


def test_checked_in_template_passes_the_guard() -> None:
    # The skeleton itself has to be clean, or every figure inherits the problem.
    assert_no_external_references(TEMPLATE_PATH.read_text(encoding="utf-8"), label="template")


# ---------------------------------------------------------------------------
# Guard 1, hardened: every bypass the audit verified now fails
#
# One test per bypass. Each of these inputs passed the guard before it was
# hardened, and each would have shipped a figure that renders blank in the
# iframe and as a bare file: the silent failure the guard exists to prevent.
# Inputs avoid "//" and "http" wherever the bypass is about something else, so
# that each test exercises the one rule that closes it.
# ---------------------------------------------------------------------------


def _refused(document: str) -> str:
    with pytest.raises(ExternalReferenceError) as excinfo:
        assert_no_external_references(document, label="bypass.html")
    return str(excinfo.value)


def test_guard_refuses_an_unquoted_script_src() -> None:
    message = _refused("<script src=//cdn.jsdelivr.net/npm/d3@7></script>")
    assert "cdn.jsdelivr.net" in message


def test_guard_refuses_an_unquoted_link_href() -> None:
    message = _refused("<link rel=stylesheet href=//fonts.example/x.css>")
    assert "fonts.example" in message


def test_guard_refuses_an_href_on_an_svg_image() -> None:
    message = _refused(f'<svg {SVG_NS}><image href="logo.png" width="10" height="10"/></svg>')
    assert "logo.png" in message
    assert "loading href" in message


def test_guard_refuses_an_xlink_href_on_an_svg_image() -> None:
    document = (
        f'<svg {SVG_NS} xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<image xlink:href="logo.png" width="10" height="10"/></svg>'
    )
    assert "logo.png" in _refused(document)


def test_guard_refuses_an_href_on_an_svg_use_pointing_at_another_file() -> None:
    message = _refused(f'<svg {SVG_NS}><use href="sprite.svg#marker"/></svg>')
    assert "sprite.svg" in message


def test_guard_refuses_css_image_set() -> None:
    message = _refused('<style>.bg{background:image-set("bg.png" 1x, "bg-2x.png" 2x)}</style>')
    assert "image-set" in message


@pytest.mark.parametrize("spelling", ["@IMPORT", "@Import", "@import"])
def test_guard_refuses_css_import_in_any_letter_case(spelling: str) -> None:
    message = _refused(f'<style>{spelling} "base.css";</style>')
    assert "@import" in message


def test_guard_refuses_a_protocol_relative_url_in_a_js_string() -> None:
    message = _refused("<script>var cdn = '//cdn.example/d3.js';</script>")
    assert "protocol-relative" in message


def test_guard_refuses_a_protocol_relative_url_in_a_template_literal() -> None:
    assert "protocol-relative" in _refused("<script>var cdn = `//cdn.example/d3.js`;</script>")


def test_guard_refuses_escaped_slashes_in_a_js_string() -> None:
    message = _refused('<script>var u = "https:\\/\\/cdn.example\\/d3.js";</script>')
    assert "external URL" in message
    assert "cdn.example" in message


def test_guard_refuses_unicode_escaped_slashes() -> None:
    assert "external URL" in _refused('<script>var u = "https:\\u002f\\u002fcdn.example";</script>')


def test_guard_refuses_entity_encoded_slashes() -> None:
    message = _refused('<script src="https:&#x2f;&#x2f;cdn.example/d3.js"></script>')
    assert "external URL" in message


def test_guard_refuses_set_attribute_src_whatever_the_value() -> None:
    message = _refused('<script>s.setAttribute("src", base + "d3.js");</script>')
    assert "setAttribute" in message


def test_guard_refuses_set_attribute_href() -> None:
    assert "setAttribute" in _refused("<script>link.setAttribute('href', sheet);</script>")


def test_guard_refuses_dynamic_import_of_a_sibling_module() -> None:
    message = _refused('<script>import("./plot.js").then(draw);</script>')
    assert "import()" in message


def test_guard_refuses_fetch_of_a_variable() -> None:
    message = _refused("<script>fetch(endpoint).then(r => r.json());</script>")
    assert "fetch()" in message


def test_guard_refuses_xmlhttprequest() -> None:
    message = _refused("<script>var xhr = new XMLHttpRequest(); xhr.open('GET', target);</script>")
    assert "XMLHttpRequest" in message


def test_guard_refuses_websocket() -> None:
    assert "WebSocket" in _refused("<script>var socket = new WebSocket(wsUrl);</script>")


def test_guard_refuses_send_beacon() -> None:
    assert "sendBeacon" in _refused("<script>navigator.sendBeacon(target, payload);</script>")


def test_guard_refuses_event_source() -> None:
    assert "EventSource" in _refused("<script>var stream = new EventSource(streamPath);</script>")


def test_guard_refuses_new_url() -> None:
    message = _refused("<script>var u = new URL(path, document.baseURI);</script>")
    assert "new URL()" in message


def test_guard_refuses_an_uppercase_scheme() -> None:
    assert "external URL" in _refused('<script>var u = "HTTPS://CDN.EXAMPLE/d3.js";</script>')


def test_hardened_guard_still_allows_fragments_data_uris_and_comments() -> None:
    # Everything a self-contained figure legitimately does: fragment and data:
    # references on the loading elements, a relative "get the data" anchor
    # (navigation, not a fetch) and JS line comments after whitespace.
    document = (
        f'<svg {SVG_NS} xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<use href="#marker"/><use xlink:href="#marker"/>'
        '<image href="data:image/png;base64,AAAA" width="1" height="1"/></svg>'
        '<style>.a{background:image-set("data:image/png;base64,AAAA" 1x)}</style>'
        '<a href="example-figure.csv" download>Get the data</a>'
        "<script>\n  // a line comment\n  var h = 1; // and a trailing one\n</script>"
    )
    assert_no_external_references(document, label="ok.html")


# ---------------------------------------------------------------------------
# Guard 2: the size budget
# ---------------------------------------------------------------------------


def test_size_guard_fails_when_the_budget_is_exceeded(tmp_path: Path) -> None:
    path = tmp_path / "fat-figure.html"
    path.write_text("x" * 2048, encoding="utf-8")
    with pytest.raises(FigureSizeError) as excinfo:
        assert_within_size_budget(path, budget_bytes=1024)
    message = str(excinfo.value)
    assert "fat-figure.html" in message
    assert "2048" in message and "1024" in message


def test_size_guard_returns_the_size_when_within_budget(tmp_path: Path) -> None:
    path = tmp_path / "thin-figure.html"
    path.write_text("x" * 100, encoding="utf-8")
    assert assert_within_size_budget(path) == 100
    assert MAX_FIGURE_BYTES == 500_000


# ---------------------------------------------------------------------------
# The height message contract, asserted across both languages
# ---------------------------------------------------------------------------


def test_contract_helper_reports_what_both_sides_have_to_agree_on() -> None:
    assert height_message_contract() == {
        "type": HEIGHT_MESSAGE_TYPE,
        "keys": HEIGHT_MESSAGE_KEYS,
        "origin": FIGURE_ORIGIN,
    }


def test_template_posts_the_contracted_message_shape() -> None:
    contract = height_message_contract()
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    match = re.search(r"postMessage\(\s*\{(?P<body>[^}]*)\}", template)
    assert match is not None, "figure template no longer posts an object to its parent"
    keys = re.findall(r"(\w+)\s*:", match.group("body"))
    assert keys == list(contract["keys"])
    assert f"type: \"{contract['type']}\"" in match.group("body")
    assert "ResizeObserver" in template


def test_listener_validates_the_origin_and_reads_the_same_fields() -> None:
    contract = height_message_contract()
    listener = LISTENER_JS.read_text(encoding="utf-8")
    assert f"var FIGURE_ORIGIN = \"{contract['origin']}\";" in listener
    assert "event.origin !== FIGURE_ORIGIN" in listener
    assert f"data.type !== \"{contract['type']}\"" in listener
    # Every field the template sends is read, and no field it does not send.
    assert set(re.findall(r"data\.(\w+)", listener)) == set(contract["keys"])
    # Matching is by id against the iframe src, per CLAUDE.md section 6.4.
    assert "data.id" in listener and "frame.src" in listener


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(
    *,
    name: str = "example-figure",
    alt_text: str = 'Alt with "quotes" & an ampersand',
    inline_data: object | None = None,
    inline_script: str | None = None,
    template_path: Path = TEMPLATE_PATH,
) -> str:
    return render_figure_html(
        name=name,
        title="Example",
        alt_text=alt_text,
        svg_light=MINIMAL_SVG,
        svg_dark=MINIMAL_SVG,
        data_href="example-figure.csv",
        inline_data=inline_data,
        inline_script=inline_script,
        template_path=template_path,
    )


def test_render_substitutes_everything_and_escapes_the_alt_text() -> None:
    document = _render()
    assert "{{" not in document
    assert 'aria-label="Alt with &quot;quotes&quot; &amp; an ampersand"' in document
    assert 'var FIGURE_ID = "disambiguation-benchmark/v1.0.0/example-figure";' in document
    assert document.count(MINIMAL_SVG) == 2
    assert 'href="example-figure.csv"' in document
    assert_no_external_references(document, label="example-figure.html")


def test_render_drops_the_data_and_script_slots_when_unused() -> None:
    document = _render()
    assert "orw-data" not in document
    assert "ORW-DATA-BEGIN" not in document
    assert "ORW-SCRIPT-BEGIN" not in document
    # The template's notes are for whoever opens the template, not the reader.
    assert "ORW-TEMPLATE-NOTES" not in document
    assert document.startswith("<!doctype html>\n<html lang=\"en\">")


def test_render_inlines_data_and_script_when_given() -> None:
    document = _render(
        inline_data=[{"series": "a", "value": 1}],
        inline_script="document.title = 'ok';",
    )
    assert '<script type="application/json" id="orw-data">' in document
    assert '"series": "a"' in document
    assert "document.title = 'ok';" in document


def test_render_neutralises_a_script_close_inside_inline_data() -> None:
    document = _render(inline_data={"label": "</script><script>alert(1)</script>"})
    assert "</script><script>alert(1)" not in document
    assert "\\u003c/script" in document


def test_render_rejects_a_script_that_closes_its_own_tag() -> None:
    with pytest.raises(TemplateError):
        _render(inline_script="var a = 1; </script>")


def test_render_fails_on_an_unsubstituted_placeholder(tmp_path: Path) -> None:
    broken = tmp_path / "broken_template.html"
    broken.write_text(
        TEMPLATE_PATH.read_text(encoding="utf-8").replace(
            "<title>{{ORW_TITLE}}</title>", "<title>{{ORW_HEADLINE}}</title>"
        ),
        encoding="utf-8",
    )
    with pytest.raises(TemplateError) as excinfo:
        _render(template_path=broken)
    assert "ORW_HEADLINE" in str(excinfo.value)


def test_render_fails_when_a_template_marker_is_missing(tmp_path: Path) -> None:
    broken = tmp_path / "no_markers.html"
    broken.write_text("<html><body>{{ORW_SVG_LIGHT}}</body></html>", encoding="utf-8")
    with pytest.raises(TemplateError):
        _render(template_path=broken)


# ---------------------------------------------------------------------------
# Figure identity, embed card, sidecars
# ---------------------------------------------------------------------------


def test_figure_id_and_url_are_versioned_paths() -> None:
    assert figure_id("collision-scatter") == "disambiguation-benchmark/v1.0.0/collision-scatter"
    assert figure_url("collision-scatter", "csv") == (
        "https://figures.openresearch.wtf/disambiguation-benchmark/v1.0.0/collision-scatter.csv"
    )


@pytest.mark.parametrize("name", ["Collision Scatter", "a/b", "-leading", "under_score", ""])
def test_figure_id_rejects_names_that_are_not_path_safe(name: str) -> None:
    with pytest.raises(ValueError):
        figure_id(name)


def test_ghost_card_keeps_the_email_fallback_next_to_the_iframe() -> None:
    # Ghost emits the card verbatim to web, email and RSS, so the fallback
    # image has to be in the markup and hidden on web by site CSS.
    card = ghost_embed_card(
        "example-figure",
        iframe_title="Example figure",
        alt_text="Alt text",
        caption="Caption.",
    )
    assert card.startswith("<!--kg-card-begin: html-->")
    assert card.endswith("<!--kg-card-end: html-->")
    assert '<img class="orw-figure-fallback"' in card
    assert "example-figure.png" in card
    assert "example-figure.html" in card
    assert "example-figure.csv" in card
    assert 'style="width:100%;border:0;height:520px"' in card


def test_write_figure_csv_counts_rows_and_rejects_ragged_ones(tmp_path: Path) -> None:
    path = tmp_path / "aggregate.csv"
    assert write_figure_csv(path, ("a", "b"), [(1, 2), (3, 4)]) == 2
    assert path.read_text(encoding="utf-8") == "a,b\n1,2\n3,4\n"
    with pytest.raises(ValueError):
        write_figure_csv(path, ("a", "b"), [(1, 2, 3)])


def test_alt_text_refuses_an_entry_for_a_figure_that_does_not_exist(tmp_path: Path) -> None:
    alt_path = tmp_path / "alt_text.json"
    with pytest.raises(FileNotFoundError):
        upsert_alt_text(alt_path, "ghost-figure", "Alt text for a figure nobody built")
    assert not alt_path.exists()


def test_alt_text_merges_and_sorts(tmp_path: Path) -> None:
    alt_path = tmp_path / "alt_text.json"
    for name in ("second-figure", "first-figure"):
        (tmp_path / f"{name}.html").write_text("<html></html>", encoding="utf-8")
        upsert_alt_text(alt_path, name, f"Alt for {name}")
    assert list(read_alt_text(alt_path)) == ["first-figure", "second-figure"]
    with pytest.raises(ValueError):
        upsert_alt_text(alt_path, "first-figure", "   ")


def test_read_alt_text_rejects_a_non_object_file(tmp_path: Path) -> None:
    alt_path = tmp_path / "alt_text.json"
    alt_path.write_text('["not", "a", "map"]', encoding="utf-8")
    with pytest.raises(ValueError):
        read_alt_text(alt_path)


# ---------------------------------------------------------------------------
# The self-test figure: it must stay unmistakably synthetic
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def selftest() -> Iterator[ModuleType]:
    """Import scripts/figure_selftest.py by path; it is a script, not a module."""
    path = REPO / "scripts" / "figure_selftest.py"
    spec = importlib.util.spec_from_file_location("figure_selftest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec because the script's dataclass resolves its string
    # annotations through sys.modules while the class body runs.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[spec.name]


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_selftest_figure_labels_itself_as_a_self_test(
    selftest: ModuleType, theme_name: str
) -> None:
    svg = selftest.build_svg(theme_name)
    assert "Toolchain self-test, not a result" in svg
    assert "NOT A FINDING" in svg
    assert "SELF-TEST" in svg
    assert "figure_selftest.py" in svg
    # Nothing that could be read as a comparison of the evaluated sources.
    for source in ("Dimensions", "OpenAlex", "Crossref", "PubMed", "ROR", "kappa"):
        assert source not in svg


def test_selftest_figure_renders_a_clean_self_contained_document(
    selftest: ModuleType, tmp_path: Path
) -> None:
    document = render_figure_html(
        name=selftest.FIGURE_NAME,
        title=selftest.TITLE,
        alt_text=selftest.ALT_TEXT,
        svg_light=selftest.build_svg("light"),
        svg_dark=selftest.build_svg("dark"),
        data_href=f"{selftest.FIGURE_NAME}.csv",
    )
    path = tmp_path / f"{selftest.FIGURE_NAME}.html"
    path.write_text(document, encoding="utf-8")
    assert_no_external_references(document, label=path.name)
    assert assert_within_size_budget(path) < MAX_FIGURE_BYTES
    assert "not a result" in selftest.ALT_TEXT


def test_hardened_guard_passes_the_self_test_figure_and_the_template(
    selftest: ModuleType,
) -> None:
    # The blunt guard must not refuse the one figure the toolchain builds, nor
    # the skeleton every figure starts from.
    document = render_figure_html(
        name=selftest.FIGURE_NAME,
        title=selftest.TITLE,
        alt_text=selftest.ALT_TEXT,
        svg_light=selftest.build_svg("light"),
        svg_dark=selftest.build_svg("dark"),
        data_href=f"{selftest.FIGURE_NAME}.csv",
    )
    assert_no_external_references(document, label=f"{selftest.FIGURE_NAME}.html")
    assert_no_external_references(TEMPLATE_PATH.read_text(encoding="utf-8"), label="template")


# ---------------------------------------------------------------------------
# The deploy contract: one statement, two files, no drift
# ---------------------------------------------------------------------------


def _deploy_contract(path: Path) -> str:
    """The comment block starting at '# DEPLOY CONTRACT' and running to the
    first non-comment line."""
    lines = path.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("# DEPLOY CONTRACT")]
    assert len(starts) == 1, f"{path} must state the deploy contract exactly once"
    block: list[str] = []
    for line in lines[starts[0] :]:
        if not line.startswith("#"):
            break
        block.append(line)
    return "\n".join(block)


def test_deploy_contract_is_stated_identically_in_both_files() -> None:
    figures_copy = _deploy_contract(REPO / "src" / "disambig" / "figures.py")
    selftest_copy = _deploy_contract(REPO / "scripts" / "figure_selftest.py")
    assert figures_copy == selftest_copy
    # The two halves of the contract, so neither can be quietly dropped. Read
    # as prose, since a phrase may wrap across comment lines.
    prose = " ".join(line.lstrip("#").strip() for line in figures_copy.splitlines())
    assert "EXCEPT those whose name starts with an underscore" in prose
    assert "never deletes a previously published version directory" in prose


# The verifier's second pass found these still slipping through after the
# first hardening; each is pinned here as a refusal.
@pytest.mark.parametrize(
    "fragment",
    [
        '<script type="module">import * as d3 from "./d3.js";</script>',
        "<script type=module>import {plot} from './plot.js'</script>",
        '<script>import "./setup.js";</script>',
        '<script>var s=document.createElement("script");s["src"]="d3.js";</script>',
        "<script>Object.assign(s,{src:'d3.js'});</script>",
        '<object data="plot.svg"></object>',
        '<embed src="plot.svg">',
        '<svg><filter><feImage href="tex.png"/></filter></svg>',
        '<meta http-equiv="refresh" content="0;url=next.html">',
        "<meta http-equiv=refresh content=0;url=next.html>",
    ],
    ids=[
        "static-esm-import-star",
        "static-esm-import-named-unquoted-type",
        "static-esm-import-bare",
        "bracket-src-assignment",
        "object-assign-src",
        "object-data",
        "embed-src",
        "feimage-href",
        "meta-refresh-quoted",
        "meta-refresh-unquoted",
    ],
)
def test_guard_refuses_second_pass_bypasses(fragment: str) -> None:
    from disambig.figures import ExternalReferenceError, assert_no_external_references

    with pytest.raises(ExternalReferenceError):
        assert_no_external_references(f"<!doctype html><html><body>{fragment}</body></html>",
                                      label="bypass")
