r"""Regression coverage for the /pos/ service-worker shell cache.

The bug this guards against: SHELL_URLS files (app.css, tokens.css, JS, fonts,
icons) are cached CACHE-FIRST by the service worker under SHELL_VERSION. If a
shell file's content changes but SHELL_VERSION isn't bumped, every browser that
already installed the old service worker keeps serving the STALE cached file
forever — e.g. new icon-sizing CSS rules landing in app.css never reach anyone
whose browser already cached shell v4. (This is exactly what happened: `.ic`
sizing/colour rules were added to app.css without bumping SHELL_VERSION, so
already-cached browsers rendered the raw, unsized, default-black icon glyphs.)

`sw-manifest.json` pins a sha256 per shell file for the CURRENT SHELL_VERSION.
Whenever a shell file's content changes, this test fails until someone bumps
SHELL_VERSION *and* regenerates the manifest — the two must move together.

Regenerate the manifest after intentionally changing a shell file:

    python3 - <<'EOF'
    import hashlib, json, re
    sw = open("static/pos/js/sw.js", encoding="utf-8").read()
    version = re.search(r'SHELL_VERSION\s*=\s*"([^"]+)"', sw).group(1)
    urls = re.findall(r'"(/static/[^"]+)"', sw)
    hashes = {u: hashlib.sha256(open("." + u, "rb").read()).hexdigest() for u in urls}
    json.dump({"version": version, "hashes": hashes},
              open("static/pos/js/sw-manifest.json", "w", encoding="utf-8"),
              indent=2, sort_keys=True)
    EOF

(Bump SHELL_VERSION in sw.js FIRST, then run the snippet above.)
"""

import json
import re
from hashlib import sha256
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SW_PATH = BASE_DIR / "static" / "pos" / "js" / "sw.js"
MANIFEST_PATH = BASE_DIR / "static" / "pos" / "js" / "sw-manifest.json"
APP_CSS_PATH = BASE_DIR / "static" / "pos" / "app.css"


def _parse_sw():
    src = SW_PATH.read_text(encoding="utf-8")
    version = re.search(r'SHELL_VERSION\s*=\s*"([^"]+)"', src).group(1)
    urls = re.findall(r'"(/static/[^"]+)"', src)
    return version, urls


def test_shell_manifest_matches_sw_version():
    version, _ = _parse_sw()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["version"] == version, (
        f"sw.js SHELL_VERSION={version!r} but sw-manifest.json is still "
        f"{manifest['version']!r} — bump the version AND regenerate the manifest "
        "together (see this file's module docstring)."
    )


def test_shell_files_unchanged_since_manifest_was_recorded():
    """Every SHELL_URLS file's hash must match the manifest exactly. A mismatch
    means the file's content drifted without SHELL_VERSION being bumped — the
    precise bug class that shipped stale, unsized icon CSS to already-cached
    browsers."""
    version, urls = _parse_sw()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["version"] == version  # covered above too; keeps this test standalone

    for url in urls:
        path = BASE_DIR / url.lstrip("/")
        assert path.exists(), f"{url} is listed in SHELL_URLS but the file is missing"
        actual = sha256(path.read_bytes()).hexdigest()
        expected = manifest["hashes"].get(url)
        assert actual == expected, (
            f"{url} changed but SHELL_VERSION/manifest weren't updated — browsers "
            "that already cached the old shell will keep serving the stale file "
            "forever. Bump SHELL_VERSION in sw.js and regenerate the manifest."
        )


def test_icon_css_rule_sizes_and_colors_icons():
    """The other half of the original bug: guard that `.ic` actually constrains
    size and inherits colour, so a raw <svg><use> glyph never renders at its
    unconstrained intrinsic size again."""
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    m = re.search(r"\.ic\s*\{([^}]*)\}", css)
    assert m, ".ic base rule is missing from app.css"
    rule = m.group(1)
    assert "width" in rule and "height" in rule, ".ic must constrain both dimensions"
    assert "currentColor" in rule, ".ic must inherit colour via currentColor"
