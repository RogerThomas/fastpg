"""LISTEN/NOTIFY: listen() on pools and connections, notify() on pools, connections and
transactions.

A session receives its own notifications before the answer to the statement that sent them,
so a listener's connection notifying itself queues them deterministically.
"""

import asyncio
import math
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, NamedTuple, assert_type, cast

import msgspec
import pydantic
import pytest
from conftest import LoopErrors

import fastpg


class Order(msgspec.Struct):
    id: int


class OrderTuple(NamedTuple):
    id: int


@dataclass
class OrderData:
    id: int


class OrderModel(pydantic.BaseModel):
    id: int


class AliasedModel(pydantic.BaseModel):
    order_id: int = pydantic.Field(alias="id")


class Reading(msgspec.Struct):
    value: float


@dataclass
class ReadingData:
    value: float


class ReadingModel(pydantic.BaseModel):
    value: float


async def _pid(conn: fastpg.Connection) -> int:
    return (await conn.select_one("SELECT pg_backend_pid()"))[0]


async def _channels(target: fastpg.Connection | fastpg.Pool) -> list[str]:
    rows = await target.select_many("SELECT pg_listening_channels() ORDER BY 1")
    return [row[0] for row in rows]


async def _next[T](listener: fastpg.Listener[T]) -> fastpg.Notification[T]:
    async with asyncio.timeout(5):
        return await anext(listener)


async def _notify_and_roll_back(conn: fastpg.Connection) -> None:
    async with conn.transaction() as tx:
        await tx.notify("channel", "rolled-back")
        msg = "error"
        raise RuntimeError(msg)


async def _drain(listener: fastpg.Listener, received: list[str]) -> None:
    """Note each payload until the listener raises."""
    async with asyncio.timeout(5):
        async for notification in listener:
            received.append(notification.payload)


async def _listen_until_entered(pool: fastpg.Pool, entered: asyncio.Event) -> None:
    async with pool.listen("channel"):
        entered.set()


async def _collect(
    pool: fastpg.Pool,
    entered: asyncio.Event,
    arrived: asyncio.Event,
    received: list[str],
) -> None:
    """Listen on "channel", noting each payload, until the listener ends."""
    async with pool.listen("channel") as listener:
        entered.set()
        async for notification in listener:
            received.append(notification.payload)
            arrived.set()


@pytest.fixture(name="other")
async def _other(dsn: str) -> AsyncIterator[fastpg.Connection]:
    """A second connection, to send notifications from."""
    async with await fastpg.connect(dsn) as connection:
        yield connection


@pytest.fixture(name="pool")
async def _pool(dsn: str) -> AsyncIterator[fastpg.Pool]:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=2) as pool:
        yield pool


async def test_a_notification_from_another_connection_arrives(
    conn: fastpg.Connection,
    other: fastpg.Connection,
) -> None:
    async with conn.listen("channel") as listener:
        await other.notify("channel", "payload")
        notification = await _next(listener)
    assert notification == fastpg.Notification("channel", "payload", await _pid(other))


async def test_a_pool_listener_receives_from_the_pool(pool: fastpg.Pool) -> None:
    async with pool.listen("channel") as listener:
        await pool.notify("channel", "payload")
        await pool.notify("channel")
        first, second = await _next(listener), await _next(listener)
    assert (first.channel, first.payload) == ("channel", "payload")
    assert (second.channel, second.payload) == ("channel", "")


async def test_one_listener_takes_several_channels(
    pool: fastpg.Pool,
    other: fastpg.Connection,
) -> None:
    async with pool.listen("one", "two", "three") as listener:
        for channel in ("two", "elsewhere", "three", "one"):
            await other.notify(channel, channel)
        received = [(await _next(listener)).channel for _ in range(3)]
    assert received == ["two", "three", "one"]


@pytest.mark.parametrize(
    ("channel", "near_miss"),
    [("Orders", "orders"), ("a b", "a_b"), ('say "hi"', "say hi"), ("x'y", "xy")],
)
async def test_channel_names_are_exact(
    conn: fastpg.Connection,
    other: fastpg.Connection,
    channel: str,
    near_miss: str,
) -> None:
    async with conn.listen(channel) as listener:
        await other.notify(near_miss, "near-miss")
        await other.notify(channel, "exact")
        notification = await _next(listener)
    assert (notification.channel, notification.payload) == (channel, "exact")


async def test_a_transaction_notifies_only_once_committed(
    conn: fastpg.Connection,
    other: fastpg.Connection,
    dsn: str,
) -> None:
    async with conn.listen("channel") as listener:
        with pytest.raises(RuntimeError, match="error"):
            await _notify_and_roll_back(other)
        async with await fastpg.connect(dsn) as third, other.transaction() as tx:
            await tx.notify("channel", "committed")
            await third.notify("channel", "sent-during-the-transaction")
        received = [(await _next(listener)).payload for _ in range(2)]
    assert received == ["sent-during-the-transaction", "committed"]


async def test_a_pool_transaction_notifies_on_commit(
    pool: fastpg.Pool,
    other: fastpg.Connection,
) -> None:
    async with other.listen("channel") as listener:
        async with pool.transaction() as tx:
            await tx.notify("channel", "committed")
        notification = await _next(listener)
    assert notification.payload == "committed"


async def test_a_connection_listener_unlistens_on_exit(conn: fastpg.Connection) -> None:
    async with conn.listen("one", "two"):
        assert await _channels(conn) == ["one", "two"]
    assert await _channels(conn) == []


async def test_a_channel_another_listener_holds_stays_listened(
    conn: fastpg.Connection,
) -> None:
    async with conn.listen("shared", "own") as outer:
        async with conn.listen("shared"):
            pass
        assert await _channels(conn) == ["own", "shared"]
        await conn.notify("shared", "still-here")
        notification = await _next(outer)
    assert notification.payload == "still-here"
    assert await _channels(conn) == []


async def test_both_listeners_of_a_channel_receive_it(conn: fastpg.Connection) -> None:
    async with conn.listen("channel") as first, conn.listen("channel") as second:
        await conn.notify("channel", "payload")
        payloads = [(await _next(first)).payload, (await _next(second)).payload]
    assert payloads == ["payload", "payload"]


async def test_a_pool_listener_gives_its_connection_back(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        async with pool.listen("channel"):
            stats = pool.stats()
            assert (stats.size, stats.acquired, stats.idle) == (1, 1, 0)
        stats = pool.stats()
        assert (stats.size, stats.acquired, stats.idle) == (1, 0, 1)
        assert await _channels(pool) == []  # the pool's only connection


async def test_breaking_out_early_still_cleans_up(
    dsn: str,
    other: fastpg.Connection,
) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        async with pool.listen("channel") as listener:
            for payload in ("1", "2", "3"):
                await other.notify("channel", payload)
            async for notification in listener:
                assert notification.payload == "1"
                break
        assert pool.stats().acquired == 0
        assert await _channels(pool) == []
        with pytest.raises(fastpg.InterfaceError, match="listen\\(\\) block ended"):
            await anext(listener)


async def test_a_task_waiting_on_a_listener_wakes_when_its_block_ends(
    conn: fastpg.Connection,
) -> None:
    async with conn.listen("channel") as listener:
        waiting = asyncio.create_task(_next(listener))
        await conn.select_one("SELECT 1")  # the task is waiting by now
    with pytest.raises(fastpg.InterfaceError, match="listen\\(\\) block ended"):
        await waiting


async def test_pool_listen_waits_up_to_max_wait(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1, max_wait=0.05) as pool:
        async with pool.acquire():
            with pytest.raises(fastpg.PoolTimeoutError, match="max_wait"):
                async with pool.listen("channel"):
                    pass
            assert pool.stats().waiting_acquire == 0
        assert pool.stats().idle == 1


async def test_pool_listen_waits_for_a_connection(dsn: str) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1, max_wait=None) as pool:
        entered = asyncio.Event()
        async with pool.acquire():
            task = asyncio.create_task(_listen_until_entered(pool, entered))
            async with asyncio.timeout(5):
                while pool.stats().waiting_acquire == 0:
                    await asyncio.sleep(0)
            assert not entered.is_set()
        await task
        assert entered.is_set()
        assert pool.stats().acquired == 0


async def test_a_listening_connection_still_runs_queries(conn: fastpg.Connection) -> None:
    async with conn.listen("channel") as listener:
        assert await conn.select_one("SELECT 1") == (1,)
        await conn.notify("channel", "own")
        results = await asyncio.gather(*(conn.select_one("SELECT $1::int", i) for i in range(5)))
        notification = await _next(listener)
        assert results == [(i,) for i in range(5)]
    assert notification == fastpg.Notification("channel", "own", await _pid(conn))


async def test_a_dropped_connection_delivers_the_queue_then_raises(
    dsn: str,
    other: fastpg.Connection,
) -> None:
    received: list[str] = []
    async with await fastpg.connect(dsn) as victim, victim.listen("channel") as listener:
        for payload in ("1", "2", "3"):
            await victim.notify("channel", payload)  # queued before it returns
        await other.select_one("SELECT pg_terminate_backend($1)", await _pid(victim))
        with pytest.raises(fastpg.ConnectionClosedError, match="connection lost"):
            await _drain(listener, received)
    assert received == ["1", "2", "3"]


async def test_closing_the_connection_ends_its_listener(conn: fastpg.Connection) -> None:
    async with conn.listen("channel") as listener:
        waiting = asyncio.create_task(_next(listener))
        await conn.close()
        with pytest.raises(fastpg.ConnectionClosedError, match="connection is closed"):
            await waiting


async def test_overflow_raises_and_says_how_many_were_queued(conn: fastpg.Connection) -> None:
    async with conn.listen("channel", max_queued=2) as listener:
        for payload in ("1", "2", "3"):
            await conn.notify("channel", payload)
        for _ in range(2):  # and keeps raising
            with pytest.raises(fastpg.ListenerOverflowError, match="2 notifications"):
                await _next(listener)


async def test_closing_the_pool_ends_an_open_listener(
    dsn: str,
    loop_errors: LoopErrors,
) -> None:
    pool = await fastpg.create_pool(dsn, min_size=1, max_size=2)
    entered, arrived = asyncio.Event(), asyncio.Event()
    received: list[str] = []
    task = asyncio.create_task(_collect(pool, entered, arrived, received))
    async with asyncio.timeout(5):
        await entered.wait()
        await pool.notify("channel", "before-close")
        await arrived.wait()
        await pool.close()
        with pytest.raises(fastpg.ConnectionClosedError):
            await task
    assert received == ["before-close"]
    assert pool.stats().size == 0
    assert loop_errors.messages() == []


@pytest.mark.parametrize(
    ("channels", "max_queued", "match"),
    [
        ((), 10_000, "at least one channel"),
        (("channel",), 0, "max_queued"),
        (("",), 10_000, "invalid SQL identifier"),
        (("x" * 64,), 10_000, "longer than 63 bytes"),
    ],
)
async def test_bad_arguments_are_refused_before_a_connection_is_taken(
    dsn: str,
    channels: tuple[str, ...],
    max_queued: int,
    match: str,
) -> None:
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        with pytest.raises(ValueError, match=match):
            async with pool.listen(*channels, max_queued=max_queued):
                pass
        assert pool.stats().acquired == 0


@pytest.mark.parametrize(
    "payload",
    [
        Order(1),
        OrderData(1),
        OrderModel(id=1),
        AliasedModel(id=1),
        {"id": 1},
        MappingProxyType({"id": 1}),
    ],
    ids=["struct", "dataclass", "pydantic", "pydantic-alias", "dict", "mapping"],
)
async def test_notify_sends_what_params_takes_as_json(
    pool: fastpg.Pool,
    other: fastpg.Connection,
    payload: fastpg.NamedParams,
) -> None:
    async with other.listen("channel") as text, other.listen("channel", into=dict) as data:
        await pool.notify("channel", payload)
        await other.notify("channel", payload)
        texts = [(await _next(text)).payload for _ in range(2)]
        dicts = [(await _next(data)).payload for _ in range(2)]
    assert texts == ['{"id":1}'] * 2
    assert dicts == [{"id": 1}] * 2


@pytest.mark.parametrize(
    ("into", "expected"),
    [
        (dict, {"id": 1}),
        (Order, Order(1)),
        (OrderTuple, OrderTuple(1)),
        (OrderModel, OrderModel(id=1)),
        (AliasedModel, AliasedModel(id=1)),
    ],
    ids=["dict", "struct", "named-tuple", "pydantic", "pydantic-alias"],
)
async def test_into_takes_what_queries_take(
    pool: fastpg.Pool,
    other: fastpg.Connection,
    into: type[Any],
    expected: object,
) -> None:
    async with pool.listen("channel", into=into) as listener:
        await other.notify("channel", Order(1))
        await other.notify("channel", '{"id": 1}')
        received = [(await _next(listener)).payload for _ in range(2)]
    assert received == [expected, expected]


async def test_into_types_the_listener(conn: fastpg.Connection) -> None:
    async with (
        conn.listen("channel") as default,
        conn.listen("channel", into=str) as text,
        conn.listen("channel", into=dict) as data,
        conn.listen("channel", into=Order) as struct,
        conn.listen("channel", into=OrderModel) as model,
    ):
        assert_type(default, fastpg.Listener[str])
        assert_type(text, fastpg.Listener[str])
        assert_type(data, fastpg.Listener[dict[str, Any]])
        assert_type(struct, fastpg.Listener[Order])
        assert_type(model, fastpg.Listener[OrderModel])
        await conn.notify("channel", Order(1))
        notification = await _next(struct)
        assert_type(notification, fastpg.Notification[Order])
        assert (await _next(model)).payload == OrderModel(id=1)
    assert notification == fastpg.Notification("channel", Order(1), await _pid(conn))


async def test_into_str_gives_the_text_as_it_was_sent(conn: fastpg.Connection) -> None:
    async with conn.listen("channel", into=str) as listener:
        await conn.notify("channel", '{"id": 1}')
        await conn.notify("channel", "not-json")
        received = [(await _next(listener)).payload for _ in range(2)]
    assert received == ['{"id": 1}', "not-json"]


@pytest.mark.parametrize(
    ("into", "bad", "cause"),
    [
        (Order, "not-json", msgspec.DecodeError),
        (Order, '{"id": "x"}', msgspec.ValidationError),
        (dict, "not-json", msgspec.DecodeError),
        (dict, "[1]", msgspec.ValidationError),
        (OrderTuple, "not-json", msgspec.DecodeError),
        (OrderTuple, '{"id": "x"}', msgspec.ValidationError),
        (OrderModel, "not-json", pydantic.ValidationError),
        (OrderModel, '{"id": "x"}', pydantic.ValidationError),
    ],
    ids=[
        "struct-bad-json",
        "struct-invalid",
        "dict-bad-json",
        "dict-invalid",
        "named-tuple-bad-json",
        "named-tuple-invalid",
        "pydantic-bad-json",
        "pydantic-invalid",
    ],
)
async def test_a_payload_that_does_not_decode_raises_and_the_listener_goes_on(
    conn: fastpg.Connection,
    into: type[Any],
    bad: str,
    cause: type[Exception],
) -> None:
    async with conn.listen("channel", into=into) as listener:
        await conn.notify("channel", bad)
        await conn.notify("channel", '{"id": 2}')
        with pytest.raises(fastpg.DecodeError, match="on channel 'channel'") as exc:
            await _next(listener)
        notification = await _next(listener)
    assert isinstance(exc.value.__cause__, cause)
    assert notification.payload == into(id=2)


@pytest.mark.parametrize(
    ("payload", "found"),
    [
        ({"x": math.nan}, r"nan at \$\.x"),
        (Reading(-math.inf), r"-inf at \$\.value"),
        (ReadingData(math.inf), r"inf at \$\.value"),
        (ReadingModel(value=math.nan), r"nan at \$\.value"),
    ],
    ids=["dict", "struct", "dataclass", "pydantic"],
)
async def test_notify_refuses_nan_and_infinities(
    pool: fastpg.Pool,
    conn: fastpg.Connection,
    payload: fastpg.NamedParams,
    found: str,
) -> None:
    async with conn.listen("channel") as listener:
        with pytest.raises(fastpg.ParameterError, match=f"{found} has no JSON form"):
            await pool.notify("channel", payload)
        with pytest.raises(fastpg.ParameterError, match=f"{found} has no JSON form"):
            await conn.notify("channel", payload)
        await conn.notify("channel", "after")
        notification = await _next(listener)
    assert notification.payload == "after"  # nothing was sent before it


async def test_notify_refuses_what_params_refuses(
    pool: fastpg.Pool,
    conn: fastpg.Connection,
) -> None:
    untyped_caller: Any = OrderTuple(1)  # what an untyped caller could pass
    match = r"payload must be a str, or what params= takes: params= must be"
    async with conn.listen("channel") as listener:
        with pytest.raises(TypeError, match=match):
            await pool.notify("channel", untyped_caller)
        with pytest.raises(TypeError, match=match):
            await conn.notify("channel", untyped_caller)
        await conn.notify("channel", "after")
        notification = await _next(listener)
    assert notification.payload == "after"  # nothing was sent before it


async def test_a_transaction_notifies_a_struct_on_commit(
    conn: fastpg.Connection,
    other: fastpg.Connection,
) -> None:
    async with conn.listen("channel", into=Order) as listener:
        async with other.transaction() as tx:
            await tx.notify("channel", Order(1))
        notification = await _next(listener)
    assert notification.payload == Order(1)


async def test_into_queries_refuse_is_refused_before_listening(
    dsn: str,
    conn: fastpg.Connection,
) -> None:
    match = r"into= must be dict, a NamedTuple, or a msgspec\.Struct or pydantic\.BaseModel"
    with pytest.raises(TypeError, match=match):
        await conn.select_one("SELECT 1 AS id", into=OrderData)
    with pytest.raises(TypeError, match=match):
        async with conn.listen("channel", into=OrderData):
            pass
    assert await _channels(conn) == []
    async with await fastpg.create_pool(dsn, min_size=1, max_size=1) as pool:
        with pytest.raises(TypeError, match=match):
            async with pool.listen("channel", into=OrderData):
                pass
        assert pool.stats().acquired == 0


async def _listen(target: fastpg.Connection | fastpg.Pool) -> None:
    async with target.listen("channel"):
        pass


async def _notify(target: fastpg.Connection | fastpg.Pool) -> None:
    await target.notify("channel")


async def _anext(listener: fastpg.Listener) -> fastpg.Notification:
    return await anext(listener)


async def _open_into(dsn: str, holder: list[object]) -> None:
    holder.append(await fastpg.connect(dsn))
    holder.append(await fastpg.create_pool(dsn, min_size=1, max_size=1))


@pytest.mark.parametrize("use", [_listen, _notify], ids=["listen", "notify"])
@pytest.mark.parametrize("target", [0, 1], ids=["connection", "pool"])
def test_use_from_another_loop_is_refused(
    dsn: str,
    use: Callable[[fastpg.Connection | fastpg.Pool], Coroutine[Any, Any, None]],
    target: int,
) -> None:
    loop = asyncio.new_event_loop()
    try:
        holder: list[object] = []
        loop.run_until_complete(_open_into(dsn, holder))
        conn, pool = cast(fastpg.Connection, holder[0]), cast(fastpg.Pool, holder[1])
        with pytest.raises(fastpg.InterfaceError, match="belongs to a different event loop"):
            asyncio.run(use(conn if target == 0 else pool))
        assert pool.stats().acquired == 0
        loop.run_until_complete(use(conn if target == 0 else pool))  # on its own loop, it works
        loop.run_until_complete(conn.close())
        loop.run_until_complete(pool.close())
    finally:
        loop.close()


def test_a_listener_used_from_another_loop_is_refused(dsn: str) -> None:
    loop = asyncio.new_event_loop()
    try:
        holder: list[object] = []
        loop.run_until_complete(_open_into(dsn, holder))
        conn, pool = cast(fastpg.Connection, holder[0]), cast(fastpg.Pool, holder[1])
        block = conn.listen("channel")
        listener = loop.run_until_complete(block.__aenter__())
        with pytest.raises(fastpg.InterfaceError, match="belongs to a different event loop"):
            asyncio.run(_anext(listener))
        loop.run_until_complete(conn.notify("channel", "payload"))
        assert loop.run_until_complete(anext(listener)).payload == "payload"
        loop.run_until_complete(block.__aexit__(None, None, None))
        loop.run_until_complete(conn.close())
        loop.run_until_complete(pool.close())
    finally:
        loop.close()
