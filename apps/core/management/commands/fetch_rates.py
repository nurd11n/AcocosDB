"""Pull today's FX rates and upsert them as ExchangeRate rows so the
«≈ X сом» figures and report totals stay current without anyone typing
rates by hand.

PRIMARY SOURCE — Frankfurter (api.frankfurter.dev), pinned to the NBKR
provider for KGS. nbkr.kg itself blocks this server's outbound IP (see
NBKR_PROXY's history in settings), so the daily pull moved behind
Frankfurter's API instead. Confirmed via GET /v2/currency/KGS that NBKR is
one of Frankfurter's own KGS data providers, so pinning ?providers=NBKR
keeps the stored rate genuinely NBKR-sourced (source=nbkr, «курс НБКР»
still shown everywhere it's shown today) — only the transport changed,
never the data's origin, and NEVER Frankfurter's own default blended rate
(its docs warn the blended figure's trailing decimals can shift as more
providers report in, which would break the frozen-rate guarantee every
payment in this app relies on).

CROSS-CHECK (optional, best-effort, never saved) — a direct nbkr.kg pull
attempted with a plain browser User-Agent (the usual reason a script gets
blocked; NBKR_PROXY remains available here too as a second workaround).
Its only job is to catch a PARSER bug: if it diverges >2% from the primary
value, that's almost always a bug in one of the two feeds, not a real
market move — logged loudly, nothing is changed.

Hard rules (see CLAUDE.md):
- A rate that jumps >10% from the last known value reads as a bad VALUE,
  not a real move — rejected outright, nothing saved, logged for the Owner
  to set by hand if it turns out to be genuine.
- On ANY failure for a currency (network down, bad JSON, oversized body, a
  redirect, a non-numeric/non-positive value) keep its last known rate:
  log loudly, change nothing for that currency, never crash the pipeline —
  one bad currency never blocks the others, and a sale must never break
  because rates couldn't refresh.
- Never follow a redirect. An obeyed redirect on a server-side fetch is
  exactly how it becomes SSRF against something like a cloud metadata
  endpoint or localhost — any 30x response is an outright failure here.

Scheduled: the `scheduler` container runs this daily before the report.
The only OTHER caller is the staff-triggered POS «Обновить» button
(apps.pos.views.refresh_rates) — a synchronous fetch kicked off by an
explicit POST, never something that runs automatically inside a normal
page-view request cycle.
Run manually: python manage.py fetch_rates
"""

import json
import logging
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.currency import CURRENCY_CODES
from apps.core.models import ExchangeRate, RateChangeLog

logger = logging.getLogger(__name__)

# --- Frankfurter (primary) --------------------------------------------------

# Hardcoded — NEVER built from user input, DB content, or an operator-settable
# env var (that shape is exactly how an outbound fetch turns into SSRF). The
# only variable part of the URL is the currency code, and that's validated
# against our own CURRENCY_CODES enum before it's ever interpolated in.
FRANKFURTER_HOST = "https://api.frankfurter.dev"
# Confirmed via GET /v2/currency/KGS: NBKR is one of Frankfurter's own KGS
# providers. Pinning it explicitly (never the default blended rate) is what
# keeps the stored rate genuinely official NBKR data — see the source label
# logic in fetch_frankfurter_rates below.
FRANKFURTER_KGS_PROVIDER = "NBKR"

_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 5
_MAX_RETRIES = 2
_MAX_BODY_BYTES = 64 * 1024
_MAX_RATE_DEVIATION_PCT = Decimal("0.10")  # reject a jump >10% from the last known rate
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


def _frankfurter_url(currency: str) -> str:
    """The pinned-provider rate URL for `currency` -> settings.CURRENCY.
    `currency` MUST already be one of our own configured codes — refuses
    anything else rather than building a URL from it."""
    if currency not in CURRENCY_CODES or currency == settings.CURRENCY:
        raise ValueError(f"refusing to fetch a rate for an unconfigured currency: {currency!r}")
    return (
        f"{FRANKFURTER_HOST}/v2/rate/{currency}/{settings.CURRENCY}"
        f"?providers={FRANKFURTER_KGS_PROVIDER}"
    )


def _get_json_once(url: str) -> dict:
    """One GET under every outbound-fetch hardening rule this app requires:
    no redirects followed (any 30x is an outright failure, never silently
    followed), strict TLS (default verify=True, never disabled, no custom
    CA), tight timeouts, and the response body capped at 64KB read directly
    off the wire rather than checked after a full download (a misbehaving
    server could otherwise send gigabytes with no Content-Length).

    On a non-2xx status, the real status code and a body snippet are logged
    here (not just the bare exception message raise_for_status produces) —
    the earlier version only ever surfaced a free-text log line, leaving the
    on-demand «Обновить» button unable to tell staff anything but a guess."""
    with requests.get(
        url,
        timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        allow_redirects=False,
        verify=True,
        stream=True,
    ) as resp:
        if 300 <= resp.status_code < 400:
            raise requests.RequestException(f"refusing to follow a redirect ({resp.status_code})")
        raw = resp.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
        if len(raw) > _MAX_BODY_BYTES:
            raise ValueError("response body exceeded the 64KB cap")
        if resp.status_code >= 400:
            logger.error(
                "fetch_rates: HTTP %s from %s — body: %s",
                resp.status_code,
                url,
                raw[:500].decode("utf-8", errors="replace"),
            )
        resp.raise_for_status()
    return json.loads(raw.decode("utf-8"))


def _get_json_capped(url: str) -> dict:
    """`_get_json_once` with up to 2 retries — ONLY for genuinely transient
    failures (connection errors, timeouts, 5xx). A redirect, a 4xx, or a
    malformed body is a permanent failure for this attempt and is never
    retried."""
    last_exc: Exception | None = None
    for _attempt in range(_MAX_RETRIES + 1):
        try:
            return _get_json_once(url)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
        except requests.HTTPError as exc:
            if getattr(exc.response, "status_code", None) not in _RETRYABLE_STATUS:
                raise
            last_exc = exc
    raise last_exc


def _fetch_frankfurter_rate(currency: str) -> Decimal:
    """The single currency's rate from the pinned-provider Frankfurter
    endpoint, as a Decimal built from the JSON numeral's own text — never
    float() on money. Rejects anything that isn't a clean positive number
    (missing, null, a string, zero, negative) without guessing."""
    data = _get_json_capped(_frankfurter_url(currency))
    value = data.get("rate")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.error(
            "fetch_rates: %s — non-numeric rate in response body: %s", currency, json.dumps(data)
        )
        raise ValueError(f"Frankfurter returned a non-numeric rate for {currency}: {value!r}")
    rate = Decimal(str(value))
    if rate <= 0:
        logger.error(
            "fetch_rates: %s — non-positive rate in response body: %s", currency, json.dumps(data)
        )
        raise ValueError(f"Frankfurter returned a non-positive rate for {currency}: {rate}")
    return rate


def _failure_reason(exc: Exception) -> str:
    """A short, honest Russian phrase for the UI — never the raw exception
    text (an HTTPError's str() is English and implementation-specific), but
    specific enough to actually distinguish "network's down" from "the
    server sent garbage", which is the whole point of this fix."""
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return _("сервер курсов ответил ошибкой %(status)s") % {"status": status}
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return _("нет соединения с сервером курсов")
    if isinstance(exc, requests.RequestException):
        return _("сетевая ошибка при обращении к серверу курсов")
    return _("сервер курсов прислал некорректные данные")


def fetch_frankfurter_rates(changed_by=None) -> tuple[int, int, dict[str, dict]]:
    """Pull today's rate for every configured non-base currency from the
    pinned-provider Frankfurter endpoint and upsert ExchangeRate rows,
    returning (updated, rejected, details). Each currency is fetched and
    validated independently — a network/validation failure for one never
    blocks the others. Every ACCEPTED rate is labelled by which provider was
    actually pinned (nbkr today; never hardcoded), so the UI can never claim
    НБКР for data that isn't. A rate that deviates >10% from the last known
    value is rejected outright — logged, nothing saved — the same fail-soft
    contract as a network failure: the last known rate stays in place either
    way.

    `details` maps currency -> {"status": "changed"|"unchanged"|"failed",
    "rate": Decimal|None, "date": date|None, "reason": str|None} — the
    per-currency outcome the «Обновить» button needs to show three genuinely
    distinct messages instead of one ambiguous one. `updated`/`rejected`
    stay as plain counts for the scheduled command's summary line; note
    `updated` counts every successful save (changed OR unchanged), same as
    before — callers wanting to know whether the VALUE moved must read
    `details`.

    Finishes with a best-effort, never-saved NBKR cross-check (see
    fetch_nbkr_cross_check) — informational only, it changes nothing."""
    today = timezone.localdate()
    wanted = [c for c in CURRENCY_CODES if c != settings.CURRENCY]
    updated, rejected = 0, 0
    accepted: dict[str, Decimal] = {}
    details: dict[str, dict] = {}
    source = ExchangeRate.NBKR if FRANKFURTER_KGS_PROVIDER == "NBKR" else ExchangeRate.FRANKFURTER

    for currency in wanted:
        try:
            rate = _fetch_frankfurter_rate(currency)
        except (requests.RequestException, ValueError, UnicodeDecodeError) as exc:
            logger.error("fetch_rates: Frankfurter fetch failed for %s: %s", currency, exc)
            details[currency] = {
                "status": "failed",
                "rate": None,
                "date": None,
                "reason": _failure_reason(exc),
            }
            continue

        existing = ExchangeRate.objects.filter(currency=currency).first()
        if existing is not None and existing.rate > 0:
            deviation = abs(rate - existing.rate) / existing.rate
            if deviation > _MAX_RATE_DEVIATION_PCT:
                logger.error(
                    "fetch_rates: REJECTED %s=%s — %.1f%% away from the last known rate %s "
                    "(sanity bound %.0f%%). Nothing saved; Owner should set it by hand if "
                    "this is a genuine move.",
                    currency,
                    rate,
                    deviation * 100,
                    existing.rate,
                    _MAX_RATE_DEVIATION_PCT * 100,
                )
                rejected += 1
                details[currency] = {
                    "status": "failed",
                    "rate": None,
                    "date": None,
                    "reason": _("новый курс отличается от прежнего больше чем на 10% — нужна проверка"),
                }
                continue

        old_rate = existing.rate if existing is not None else None
        # One row per currency — overwrite it in place, stamping today as the
        # last-updated date. No dated pile-up.
        ExchangeRate.objects.update_or_create(
            currency=currency, defaults={"rate": rate, "date": today, "source": source}
        )
        accepted[currency] = rate
        updated += 1
        changed = old_rate is None or old_rate != rate
        details[currency] = {
            "status": "changed" if changed else "unchanged",
            "rate": rate,
            "date": today,
            "reason": None,
        }
        logger.info(
            "fetch_rates: accepted %s (source=%s, provider=%s, changed=%s)",
            currency,
            source,
            FRANKFURTER_KGS_PROVIDER,
            changed,
        )

        if changed:
            RateChangeLog.objects.create(
                currency=currency,
                old_rate=old_rate,
                new_rate=rate,
                source=source,
                changed_by=changed_by,
            )

    if accepted:
        _cross_check_against_nbkr(accepted)
    return updated, rejected, details


# --- NBKR (cross-check only — never the writer) -----------------------------

NBKR_URL = "https://www.nbkr.kg/XML/daily.xml"
# The usual reason an automated fetch gets blocked is a missing/non-browser
# User-Agent — try one before giving up. NBKR_PROXY (below) is the heavier
# second workaround, still available here for the cross-check only.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_CROSS_CHECK_DEVIATION_PCT = Decimal("0.02")


def _nbkr_proxies():
    """Route ONLY the NBKR cross-check request through settings.NBKR_PROXY
    when it's set (nbkr.kg blocks foreign/cloud IP ranges outright). Empty
    setting → None → a direct request, unchanged behaviour."""
    proxy = settings.NBKR_PROXY
    return {"http": proxy, "https": proxy} if proxy else None


def fetch_nbkr_cross_check() -> dict[str, Decimal]:
    """Best-effort direct nbkr.kg pull used ONLY to sanity-check the primary
    Frankfurter/NBKR values — NEVER written to ExchangeRate itself (there is
    exactly one writer of that table: fetch_frankfurter_rates above).
    Returns {} on ANY failure (network, blocked IP, bad XML) — this is
    optional and purely informational, never load-bearing."""
    try:
        resp = requests.get(
            NBKR_URL,
            timeout=20,
            proxies=_nbkr_proxies(),
            headers={"User-Agent": _BROWSER_USER_AGENT},
        )
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)  # respects the XML's own encoding
    except (requests.RequestException, ElementTree.ParseError) as exc:
        logger.info("fetch_rates: NBKR cross-check unavailable (informational only): %s", exc)
        return {}

    wanted = {c for c in CURRENCY_CODES if c != settings.CURRENCY}
    out = {}
    for node in root.findall("Currency"):
        iso = node.get("ISOCode")
        if iso not in wanted:
            continue
        try:
            nominal = Decimal((node.findtext("Nominal") or "1").replace(",", "."))
            value = Decimal((node.findtext("Value") or "").replace(",", "."))
            if nominal <= 0 or value <= 0:
                continue
            out[iso] = value / nominal
        except (InvalidOperation, ZeroDivisionError):
            continue
    return out


def _cross_check_against_nbkr(accepted: dict[str, Decimal]) -> None:
    """Compare the values just accepted from Frankfurter/NBKR against a
    direct nbkr.kg pull, when reachable. A divergence >2% almost always
    means a PARSER bug in one of the two feeds, not a real market move —
    logged loudly for investigation, never acted on (no save, no reject)."""
    cross = fetch_nbkr_cross_check()
    for currency, primary_rate in accepted.items():
        other = cross.get(currency)
        if other is None:
            continue
        deviation = abs(other - primary_rate) / primary_rate
        if deviation > _CROSS_CHECK_DEVIATION_PCT:
            logger.warning(
                "fetch_rates: cross-check divergence for %s — Frankfurter/NBKR=%s vs direct "
                "NBKR=%s (%.1f%% apart). Likely a parser bug in one of the two feeds, not a "
                "market move.",
                currency,
                primary_rate,
                other,
                deviation * 100,
            )


class Command(BaseCommand):
    help = "Fetch today's rates (Frankfurter, pinned to the NBKR provider) and upsert ExchangeRate rows."

    def handle(self, *args, **options):
        updated, rejected, _details = fetch_frankfurter_rates()
        today = timezone.localdate()
        if updated:
            msg = f"Rates for {today}: {updated} updated"
            if rejected:
                msg += f", {rejected} rejected (sanity check — set manually if genuine)"
            self.stdout.write(self.style.SUCCESS(msg))
        else:
            msg = f"Rates for {today}: nothing updated"
            if rejected:
                msg += f", {rejected} rejected (sanity check)"
            msg += " — kept last known rates."
            self.stdout.write(self.style.WARNING(msg))
