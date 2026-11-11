# Notifications

PostgreSQL's `LISTEN` and `NOTIFY` let one session send a short message on a named
channel to every session listening on it.

## Listen

`pool.listen()` listens on one or more channels for the length of the block.
Iterate over it to receive each notification:

```python
async with await fastpg.create_pool(dsn) as pool:
    async with pool.listen("orders", "jobs") as listener:
        async for n in listener:
            print(n.channel, n.payload, n.pid)  # orders new-order 4242
```

Each notification is a `Notification` with the `channel`, the `payload` and the `pid` of
the server process that sent it.

The listener holds one of the pool's connections until the block ends.
Then the pool resets it and uses it again.

`conn.listen()` does the same on a single connection.
That connection can still run queries while you listen.

Channel names are exact: `listen("Orders")` doesn't receive `notify("orders")`.

## Notify

`notify()` sends a notification. It works on a pool, a connection and a transaction:

```python
await pool.notify("orders", "new-order")
```

In a transaction, the notification is sent only when the transaction commits.
If it rolls back, nothing is sent:

```python
async with pool.transaction() as tx:
    await tx.execute("INSERT INTO orders VALUES ($1)", 42)
    await tx.notify("orders", "42")
```

So a listener never hears about an order that wasn't saved.

## Structs, models and dicts

`notify()` also takes anything [`params=`](queries.md#name-with-params) takes, and sends it
as JSON:

```python
import msgspec


class Order(msgspec.Struct):
    id: int
    total: int


await pool.notify("orders", Order(id=42, total=100))
```

Pass `into=` to `listen()` to get each payload back as that type.
`into=` takes the same types as in [queries](queries.md):

```python
async with pool.listen("orders", into=Order) as listener:
    async for n in listener:
        print(n.payload.id, n.payload.total)  # 42 100
```

Without `into=`, each payload is the text that was sent.

If a payload isn't valid for `into=`, that notification raises `DecodeError`.
The listener keeps going: the next notification arrives as usual.

## Good to know

* If the connection is lost or closed, or the pool closes, the listener first gives you
  the notifications it already received, then raises `ConnectionClosedError`.
  fastpg doesn't reconnect: open a new listener.
* A listener keeps up to 10,000 notifications you haven't read yet.
  If more arrive, it first gives you those 10,000, then raises `ListenerOverflowError`,
  because the ones after were lost.
  Pass `max_queued=` to change the limit.
* A listener only receives notifications sent after its block starts.
* `conn.listen()` raises `InterfaceError` inside a transaction: open the listener first.
