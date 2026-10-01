#!/usr/bin/env python3
"""Prove the figure toolchain end to end, before there is any data to draw.

The benchmark has no results yet, and the figure path is the part of a project
that always turns out to be broken on the day the numbers arrive: the PNG
converter is missing, the dark variant was never rendered, the CSV link points
at nothing, the alt text was forgotten. So this script builds one complete
figure from input that is hard-coded, synthetic and labelled as such, and
exercises every step a real figure will use: self-contained HTML, light and
dark SVG, 2x PNGs through rsvg-convert, the aggregate CSV, an alt text entry,
a provenance entry, and both figure guards.

The output is NOT a finding and is not about any evaluated source. It is
written to outputs/figures/_selftest/ rather than outputs/figures/, it is
labelled inside the figure itself, the CSV carries the same warning on every
row, and the numbers in it are 1, 2 and 3 arbitrary units, which is nothing
this project could ever measure. Its own alt text and provenance say the same.
Delete the directory whenever you like; rerunning this script rebuilds it.

Usage: uv run scripts/figure_selftest.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from disambig.figures import (  # noqa: E402
    CATEGORICAL,
    FIGURES_DIR,
    THEMES,
    assert_no_external_references,
    assert_within_size_budget,
    export_png,
    header_height,
    marker_shape,
    render_figure_html,
    require_theme,
    svg_document,
    svg_line,
    svg_rect,
    svg_text,
    upsert_alt_text,
    write_figure_csv,
)
from disambig.logging_setup import configure_logging, new_run_id  # noqa: E402
from disambig.provenance import ProvenanceEntry, record_provenance  # noqa: E402

log = structlog.get_logger(__name__)

SCRIPT: Final = "scripts/figure_selftest.py"
FIGURE_NAME: Final = "toolchain-selftest"

# Quarantined in its own underscore-prefixed directory so nothing here can be
# mistaken for a figure the post ships: it has its own alt_text.json and its
# own provenance.json rather than writing into the real ones.
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
OUT_DIR: Final = FIGURES_DIR / "_selftest"

# The whole point of the labelling: nobody should be able to see this figure,
# or its CSV, or its alt text, and think it says something about a source.
SYNTHETIC_WARNING: Final = "synthetic self-test input, not a measurement of anything"


@dataclass(frozen=True)
class SelfTestRow:
    """One obviously fake series. Values are whole arbitrary units."""

    label: str
    value: float
    uncertainty: float


SELFTEST_ROWS: Final = (
    SelfTestRow("Synthetic sample one", 1.0, 0.5),
    SelfTestRow("Synthetic sample two", 2.0, 0.25),
    SelfTestRow("Synthetic sample three", 3.0, 0.75),
)

TITLE: Final = "Toolchain self-test, not a result"
SUBTITLE: Final = (
    "Synthetic input hard-coded in scripts/figure_selftest.py. These bars measure nothing.",
    "Built to prove the export path: HTML, light and dark SVG, 2x PNG, CSV, alt text, provenance.",
)
ALT_TEXT: Final = (
    "Toolchain self-test figure, not a result. Three horizontal bars labelled synthetic sample "
    "one, two and three, of 1, 2 and 3 arbitrary units, each with an error bar and a different "
    "marker shape at its end, over a diagonal NOT A FINDING watermark. The values are hard-coded "
    "in scripts/figure_selftest.py and describe nothing; the figure exists only to check that "
    "the figure export path works."
)

# Geometry. Fixed rather than data-driven, because the input is fixed.
WIDTH: Final = 780
LABEL_RIGHT: Final = 230  # right edge of the label column
PLOT_LEFT: Final = 250
PLOT_WIDTH: Final = 440
ROW_HEIGHT: Final = 46
AXIS_MAX: Final = 4.0
AXIS_TICKS: Final = (0, 1, 2, 3, 4)


def _x(value: float) -> float:
    return PLOT_LEFT + (value / AXIS_MAX) * PLOT_WIDTH


def build_svg(theme_name: str) -> str:
    """Draw the self-test figure in one theme."""
    theme = require_theme(theme_name)
    top = header_height(SUBTITLE) + 18
    axis_y = top + len(SELFTEST_ROWS) * ROW_HEIGHT
    height = axis_y + 54

    body: list[str] = []

    # Gridlines and axis first, so bars sit on top of them.
    for tick in AXIS_TICKS:
        x = _x(tick)
        body.append(svg_line(x, top - 10, x, axis_y, stroke=theme["grid"]))
        body.append(
            svg_text(x, axis_y + 20, str(tick), size=12, fill=theme["muted"], anchor="middle")
        )
    body.append(
        svg_text(
            _x(AXIS_MAX / 2),
            axis_y + 40,
            "arbitrary units (synthetic)",
            size=12,
            fill=theme["muted"],
            anchor="middle",
        )
    )

    # Diagonal watermark, drawn behind the bars at low opacity. Built inline
    # rather than through svg_text because it is the only rotated, part-opaque
    # element in the toolchain and does not deserve a helper.
    centre_x = PLOT_LEFT + PLOT_WIDTH / 2
    centre_y = top + (axis_y - top) / 2
    body.append(
        f'<text x="{centre_x:.0f}" y="{centre_y:.0f}" font-size="62" font-weight="700" '
        f'text-anchor="middle" fill="{theme["accent2"]}" opacity="0.16" '
        f'transform="rotate(-14 {centre_x:.0f} {centre_y:.0f})">NOT A FINDING</text>'
    )

    for index, row in enumerate(SELFTEST_ROWS):
        colour_key, shape = CATEGORICAL[index % len(CATEGORICAL)]
        colour = theme[colour_key]
        y = top + index * ROW_HEIGHT + ROW_HEIGHT / 2
        body.append(
            svg_text(LABEL_RIGHT, y + 5, row.label, size=14, fill=theme["text"], anchor="end")
        )
        body.append(svg_rect(_x(0), y - 9, _x(row.value) - _x(0), 18, fill=colour, rx=2))
        # Uncertainty shown rather than a naked point estimate (CLAUDE.md
        # section 4), even on input this fake.
        low = _x(max(0.0, row.value - row.uncertainty))
        high = _x(min(AXIS_MAX, row.value + row.uncertainty))
        body.append(svg_line(low, y, high, y, stroke=theme["text"], stroke_width=1.5))
        for cap in (low, high):
            body.append(svg_line(cap, y - 5, cap, y + 5, stroke=theme["text"], stroke_width=1.5))
        # Shape as well as colour, so the series survives a colourblind reader
        # and a greyscale print.
        body.append(marker_shape(shape, _x(row.value), y, 5, fill=theme["text"]))
        body.append(
            svg_text(
                high + 12,
                y + 5,
                f"{row.value:.1f} ± {row.uncertainty:.2f}",
                size=13,
                fill=theme["muted"],
            )
        )

    # Badge in the top right corner, in case the title is ever cropped out.
    badge_width = 118
    body.append(
        svg_rect(WIDTH - badge_width - 16, 18, badge_width, 24, fill=theme["accent2"], rx=4)
    )
    body.append(
        svg_text(
            WIDTH - badge_width / 2 - 16,
            35,
            "SELF-TEST",
            size=13,
            # Dark ink on the orange badge in both themes: the badge colour does
            # not change, so neither can the text on it.
            fill=THEMES["light"]["text"],
            weight="700",
            anchor="middle",
            letter_spacing="0.08em",
        )
    )

    return svg_document(
        width=WIDTH,
        height=height,
        theme_name=theme_name,
        body="\n".join(body),
        title=TITLE,
        subtitle_lines=SUBTITLE,
    )


def main() -> None:
    run_id = new_run_id()
    configure_logging(PROJECT_ROOT / "logs", run_id)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    svg_light = build_svg("light")
    svg_dark = build_svg("dark")
    light_path = OUT_DIR / f"{FIGURE_NAME}.svg"
    dark_path = OUT_DIR / f"{FIGURE_NAME}-dark.svg"
    light_path.write_text(svg_light + "\n", encoding="utf-8")
    dark_path.write_text(svg_dark + "\n", encoding="utf-8")

    csv_path = OUT_DIR / f"{FIGURE_NAME}.csv"
    rows_out = write_figure_csv(
        csv_path,
        ("series", "value", "uncertainty", "unit", "note"),
        [
            (
                row.label,
                f"{row.value:.1f}",
                f"{row.uncertainty:.2f}",
                "arbitrary",
                SYNTHETIC_WARNING,
            )
            for row in SELFTEST_ROWS
        ],
    )

    document = render_figure_html(
        name=FIGURE_NAME,
        title=f"{TITLE} (disambiguation benchmark toolchain)",
        alt_text=ALT_TEXT,
        svg_light=svg_light,
        svg_dark=svg_dark,
        data_href=csv_path.name,
        data_label="Get the data (CSV, synthetic)",
    )
    html_path = OUT_DIR / f"{FIGURE_NAME}.html"
    html_path.write_text(document, encoding="utf-8")

    # render_figure_html already ran the reference guard; running both guards
    # here as well is the point of the exercise, so a broken guard fails this
    # script rather than passing quietly.
    assert_no_external_references(html_path.read_text(encoding="utf-8"), label=html_path.name)
    html_bytes = assert_within_size_budget(html_path)

    for svg_path in (light_path, dark_path):
        export_png(svg_path, svg_path.with_suffix(".png"))

    upsert_alt_text(OUT_DIR / "alt_text.json", FIGURE_NAME, ALT_TEXT)

    # Provenance goes in the self-test directory, not outputs/provenance.json:
    # a synthetic entry in the real provenance file would be a lie about the
    # artefacts the post ships with, however clearly it was labelled.
    for artefact in (html_path, light_path, dark_path, csv_path):
        record_provenance(
            OUT_DIR / "provenance.json",
            str(artefact.relative_to(PROJECT_ROOT)),
            ProvenanceEntry(
                script=SCRIPT,
                source_snapshot=f"none: {SYNTHETIC_WARNING}",
                query="SELFTEST_ROWS hard-coded in scripts/figure_selftest.py",
                rows_in=len(SELFTEST_ROWS),
                rows_out=rows_out,
                run_id=run_id,
                run_at=now,
            ),
        )

    written = sorted(path.name for path in OUT_DIR.iterdir())
    log.info(
        "selftest_figure_written",
        out_dir=str(OUT_DIR.relative_to(PROJECT_ROOT)),
        html_bytes=html_bytes,
        rows=rows_out,
        files=written,
    )
    print(
        json.dumps(
            {
                "run_id": run_id,
                "out_dir": str(OUT_DIR.relative_to(PROJECT_ROOT)),
                "html_bytes": html_bytes,
                "files": written,
                "note": SYNTHETIC_WARNING,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
