"""Phase 4, Part 0 — the two-Telegram-bots architecture.

These are structural tests: they prove the staff bot and client bot are
architecturally incapable of leaking into each other, independent of any
single handler's runtime behaviour (that's covered per-feature as each Part
of Phase 4 lands). See bot/staff_bot.py and bot/client_bot.py docstrings.
"""

import pytest
from aiogram.filters import Command, CommandStart

from bot import client_bot, staff_bot

# No django_db mark: every test here is pure introspection/monkeypatching,
# no database access.

# Names that must never appear anywhere in bot/client_bot.py's namespace: the
# staff query layer (apps.wa.replies) — which accepts an ARBITRARY phone/query
# from the caller — and the aggregate services it wraps (today's revenue
# across all clients, every debtor, etc.). client_debt is deliberately NOT
# here: CLIENT_BOTS.md §3.5 ("Мои заказы") makes a client's OWN debt, looked
# up only via client_by_chat_id (never an arbitrary identifier), a real
# feature — see menu_orders. BotUser stays banned; it's the staff allowlist.
_STAFF_ONLY_NAMES = {
    "stock_reply",
    "today_reply",
    "client_reply",
    "debts_reply",
    "restock_reply",
    "lapsed_reply",
    "today_summary",
    "debtors_report_rows",
    "low_stock_variants",
    "BotUser",
}


def _command_names(dp) -> set[str]:
    names = set()
    for handler in dp.message.handlers:
        for f in handler.filters:
            filt = f.callback
            if isinstance(filt, CommandStart):
                names.add("start")
            elif isinstance(filt, Command):
                names.update(filt.commands)
    return names


def test_client_bot_module_never_imports_the_staff_reply_layer():
    leaked = _STAFF_ONLY_NAMES & set(vars(client_bot))
    assert not leaked, f"client_bot.py imports staff-only data access: {leaked}"


def test_staff_and_client_dispatchers_share_no_command():
    staff_commands = _command_names(staff_bot.dp)
    client_commands = _command_names(client_bot.dp)
    # "start" legitimately exists on both — it's the entry point for each bot,
    # but the two cmd_start functions are different code (allowlist-gated HELP
    # vs. a public subscribe prompt). Every OTHER command must be disjoint.
    assert staff_commands - {"start"}, "staff bot registered no commands — did an import break?"
    assert client_commands - {"start"} == set(), (
        f"client bot has non-/start commands, which must not exist yet: "
        f"{client_commands - {'start'}}"
    )
    overlap = (staff_commands & client_commands) - {"start"}
    assert not overlap, f"staff and client bots share commands: {overlap}"


def test_staff_and_client_bots_use_different_dispatcher_instances():
    assert staff_bot.dp is not client_bot.dp


def test_client_bot_handlers_never_touch_aggregate_business_data():
    # CLIENT_BOTS.md §3.5 ("Мои заказы") legitimately shows a client their OWN
    # debt (client_debt, scoped to client_by_chat_id — never an arbitrary
    # identifier), so "debt" alone is no longer a banned substring — see
    # _STAFF_ONLY_NAMES above. What must NEVER appear is anything that would
    # mean a cross-client aggregate or cost/profit figure leaked in.
    import inspect

    handlers_source = "".join(inspect.getsource(h.callback) for h in client_bot.dp.message.handlers)
    for leaked in ("total_kgs", "revenue", "profit", "cost_price"):
        assert leaked not in handlers_source, f"a client_bot handler mentions '{leaked}'"


def test_staff_bot_allowlist_is_an_outer_middleware():
    """The gate must be structural — registered on the message observer itself,
    ahead of every filter and handler — not a convention each handler repeats."""
    assert any(
        isinstance(m, staff_bot.AllowlistMiddleware) for m in staff_bot.dp.message.outer_middleware
    )


def test_staff_bot_unknown_id_gets_silence_not_help(monkeypatch):
    """The middleware must swallow a message from a Telegram ID with no active
    BotUser row — the handler never runs, nothing is answered — and must pass
    an allowlisted ID through to the handler with the BotUser attached."""
    import asyncio

    from asgiref.sync import sync_to_async

    class FakeUser:
        id = 999999999

    class FakeMessage:
        from_user = FakeUser()
        text = "/debts"

    async def _run():
        middleware = staff_bot.AllowlistMiddleware()
        handled = []

        async def handler(event, data):
            handled.append(data.get("bot_user"))
            return "handled"

        # Unknown ID -> dropped before the handler, total silence.
        monkeypatch.setattr(staff_bot, "get_allowed_user", sync_to_async(lambda tid: None))
        assert await middleware(handler, FakeMessage(), {}) is None
        assert handled == []

        # Allowlisted ID -> handler runs and receives the BotUser.
        sentinel = object()
        monkeypatch.setattr(staff_bot, "get_allowed_user", sync_to_async(lambda tid: sentinel))
        monkeypatch.setattr(staff_bot, "log_message", sync_to_async(lambda *a: None))
        assert await middleware(handler, FakeMessage(), {}) == "handled"
        assert handled == [sentinel]

    asyncio.run(_run())


def test_bot_main_idles_without_polling_when_bots_disabled(monkeypatch):
    """BOTS_ENABLED=False (the shipped prod default) must never reach
    start_polling — bots are not production-ready yet. Breaks the idle loop
    via a StopIteration-raising fake sleep so the test terminates."""
    import asyncio

    from bot import main as bot_main

    monkeypatch.setattr(bot_main.settings, "BOTS_ENABLED", False)

    async def fake_sleep(_seconds):
        raise RuntimeError("idle loop tick — proves no polling ever started")

    monkeypatch.setattr(bot_main.asyncio, "sleep", fake_sleep)

    def boom(*a, **k):
        raise AssertionError("start_polling must not be called while BOTS_ENABLED=False")

    monkeypatch.setattr(bot_main.staff_bot.dp, "start_polling", boom)
    monkeypatch.setattr(bot_main.client_bot.dp, "start_polling", boom)

    with pytest.raises(RuntimeError, match="idle loop tick"):
        asyncio.run(bot_main.main())
