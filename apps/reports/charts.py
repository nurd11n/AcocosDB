"""Server-rendered SVG chart geometry.

This module only computes coordinates/paths — it emits NO colours. The template
draws the SVG with `fill/stroke="var(--...)"`, so dark/light theming is free (no
JS re-reads the palette) and the strict CSP needs no new exception. Money axis
labels are abbreviated (1,2 млн); cards/tables keep exact figures.
"""

import math


def abbrev_money(v) -> str:
    v = float(v)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}".replace(".", ",") + " млн"
    if v >= 1_000:
        return f"{round(v / 1000)} тыс"
    return f"{round(v)}"


def area_line(series, width=720, height=220):
    """Area + line for the revenue series. `series` = [{label, value}]."""
    values = [float(p["value"]) for p in series]
    n = len(series)
    pad = {"l": 6, "r": 6, "t": 10, "b": 22}
    pw = width - pad["l"] - pad["r"]
    ph = height - pad["t"] - pad["b"]
    maxv = max(values) if (values and max(values) > 0) else 1.0

    def x_at(i):
        return pad["l"] + (pw * (i / (n - 1)) if n > 1 else pw / 2)

    def y_at(v):
        return pad["t"] + ph * (1 - v / maxv)

    pts = [(x_at(i), y_at(values[i])) for i in range(n)]
    base = pad["t"] + ph
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (
        f"M{pts[0][0]:.1f},{base:.1f} L"
        + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        + f" L{pts[-1][0]:.1f},{base:.1f} Z"
    )
    grid = [
        {"y": base, "label": "0"},
        {"y": pad["t"] + ph / 2, "label": abbrev_money(maxv / 2)},
        {"y": pad["t"], "label": abbrev_money(maxv)},
    ]
    if n > 1:
        label_idx = sorted({0, n - 1, round((n - 1) / 3), round(2 * (n - 1) / 3)})
    else:
        label_idx = [0]
    xlabels = [{"x": x_at(i), "label": series[i]["label"]} for i in label_idx]
    points = [
        {"x": x_at(i), "y": y_at(values[i]), "label": series[i]["label"], "value": series[i]["value"]}
        for i in range(n)
    ]
    return {
        "width": width,
        "height": height,
        "line": line,
        "area": area,
        "grid": grid,
        "xlabels": xlabels,
        "points": points,
        "last": {"x": pts[-1][0], "y": pts[-1][1]} if pts else None,
        "base": base,
        "has_data": any(v > 0 for v in values),
    }


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
