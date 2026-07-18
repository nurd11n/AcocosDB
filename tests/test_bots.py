"""Phase 4, Part 0 — the two-Telegram-bots architecture.

These are structural tests: they prove the staff bot and client bot are
architecturally incapable of leaking into each other, independent of any
single handler's runtime behaviour (that's covered per-feature as each Part
of Phase 4 lands). See bot/staff_bot.py and bot/client_bot.py docstrings.
"""

from aiogram.filters import Command, CommandStart

from bot import client_bot, staff_bot

# No django_db mark: every test here is pure introspection/monkeypatching,
# no database access.

# Names that must never appear anywhere in bot/client_bot.py's namespace: the
# staff query layer (apps.wa.replies) and the aggregate services it wraps.
# If a future handler needs a client's OWN debt/orders, it must call a
# per-client lookup, never one of these — and this test must then be
# re-scoped deliberately, not silently pass.
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
    "client_debt",
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


def test_client_bot_start_handler_sends_no_business_data():
    # The client bot's only handlers today (Part 0) send fixed Russian copy —
    # no DB aggregate, so no revenue/debt figure can appear. This guards
    # against a future edit accidentally interpolating a dynamic value in.
    # Scoped to the actual handler bodies, not the module docstring (which
    # legitimately discusses "debt" as a future, out-of-scope concern).
    import inspect

    handlers_source = "".join(
        inspect.getsource(h.callback) for h in client_bot.dp.message.handlers
    )
    for leaked in ("total_kgs", "revenue", "profit", "cost_price", "debt"):
        assert leaked not in handlers_source, f"a client_bot handler mentions '{leaked}'"


def test_staff_bot_allowlist_is_an_outer_middleware():
    """The gate must be structural — registered on the message observer itself,
    ahead of every filter and handler — not a convention each handler repeats."""
    assert any(
        isinstance(m, staff_bot.AllowlistMiddleware)
        for m in staff_bot.dp.message.outer_middleware
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
