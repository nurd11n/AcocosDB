"""Static checks over every template file — no DB, no client, just text.

Added after a real production incident: a multi-line `{# ... #}` comment in
templates/registration/login.html rendered as literal visible text on the
login page. Root cause is a genuine Django template-engine gotcha, not a
typo — `django.template.base.tag_re` (the tokenizer's tag-matching regex) is
compiled WITHOUT re.DOTALL, so `.` in its `{#.*?#}` alternative never matches
a newline. A `{# #}` comment that spans more than one physical line is
therefore never recognized as a tag at all — the tokenizer treats the whole
thing, delimiters included, as ordinary text content, and it gets rendered.
`{% comment %}...{% endcomment %}` has no such limitation and is the fix.

This scans every .html template in the project for that exact shape so the
same class of bug can't silently ship again.
"""

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _multiline_hash_comments(path: Path) -> list[int]:
    """1-indexed line numbers where a `{#` opener's matching `#}` is on a
    later line — exactly the shape that silently fails to parse as a
    comment. Mirrors django.template.base.tag_re's own (non-DOTALL) search,
    not a general-purpose parser."""
    text = path.read_text(encoding="utf-8")
    bad = []
    for m in re.finditer(r"\{#", text):
        start = m.start()
        end = text.find("#}", start)
        if end == -1:
            continue  # unclosed entirely — a different problem, not this one
        if "\n" in text[start:end]:
            bad.append(text.count("\n", 0, start) + 1)
    return bad


def test_no_template_has_a_multiline_hash_comment():
    offenders = {}
    for path in TEMPLATES_DIR.rglob("*.html"):
        lines = _multiline_hash_comments(path)
        if lines:
            offenders[str(path.relative_to(TEMPLATES_DIR.parent))] = lines

    assert not offenders, (
        "Multi-line {# ... #} comment(s) found — these render as literal "
        "text on the page instead of being stripped (Django's tag_re has no "
        "re.DOTALL, so {#...#} never matches across a newline). Use "
        "{% comment %}...{% endcomment %} instead, which IS multi-line safe. "
        f"Offending file:line(s): {offenders}"
    )
