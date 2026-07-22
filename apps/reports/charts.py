"""Server-rendered SVG chart geometry.

This module only computes coordinates/paths — it emits NO colours. The template
draws the SVG with `fill/stroke="var(--...)"`, so dark/light theming is free (no
JS re-reads the palette) and the strict CSP needs no new exception. Money axis
labels are abbreviated (1,2 млн); cards/tables keep exact figures.
"""

import math


def donut(slices, size=160, stroke=22):
    """Part-to-whole donut (payment methods). `slices` = [{label, value}], ≤4."""
    total = sum(float(s["value"]) for s in slices) or 1.0
    r = (size - stroke) / 2
    cx = cy = size / 2
    circ = 2 * math.pi * r
    ramp = ["var(--cat-1)", "var(--cat-2)", "var(--cat-3)", "var(--cat-4)"]
    segments = []
    offset = 0.0
    for i, s in enumerate(slices):
        frac = float(s["value"]) / total
        seg_len = frac * circ
        segments.append(
            {
                "color": ramp[i % 4],
                "dash": f"{seg_len:.2f} {circ - seg_len:.2f}",
                "offset": f"{-offset:.2f}",
                "label": s["label"],
                "value": s["value"],
                "pct": round(frac * 100),
            }
        )
        offset += seg_len
    return {
        "size": size,
        "cx": cx,
        "cy": cy,
        "r": r,
        "stroke": stroke,
        "circ": circ,
        "segments": segments,
        "has_data": any(float(s["value"]) > 0 for s in slices),
    }


def bars(items):
    """Ranked horizontal bars (channels) as fractions of the max — rendered with
    CSS width%, not SVG, so they animate via transform:scaleX and theme for free.
    `items` = [{label, value}] already ranked descending."""
    maxv = max((float(i["value"]) for i in items), default=0) or 1.0
    return [
        {
            "label": i["label"],
            "value": i["value"],
            "pct": round(float(i["value"]) / maxv * 100),
            "frac": round(float(i["value"]) / maxv, 4),  # 0–1 for transform:scaleX()
        }
        for i in items
    ]
