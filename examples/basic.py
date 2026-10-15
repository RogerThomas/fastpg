#!yeet
"""Minimal fastpg example. Run: ./examples/basic.py --person-id 42"""

from typing import Annotated

import msgspec
from yeetr import Opt

import fastpg


class Person(msgspec.Struct):
    name: str
    age: int
    email: str | None


async def main(
    *,
    person_id: int = 42,
    dsn: Annotated[
        str,
        Opt(envvar="FASTPG_DSN"),
    ] = "postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg",
) -> None:
    async with await fastpg.connect(dsn) as conn:
        person = await conn.select_one(
            "SELECT name, age, email FROM people WHERE id = $1",
            person_id,
            into=Person,
        )
        print(person)

        maybe = await conn.select_one_or_none(
            "SELECT name, age, email FROM people WHERE id = $1",
            -1,
            into=Person,
        )
        print("missing row ->", maybe)

        # A t-string query: interpolations become bound parameters ($1, $2), never SQL text.
        min_age, limit = 70, 3
        adults = await conn.select_many(
            t"SELECT name, age, email FROM people WHERE age >= {min_age} ORDER BY id LIMIT {limit}",
            into=Person,
        )
        print(adults)

        try:
            await conn.select_one("SELECT name FROM people WHERE id = $1", person_id, into=Person)
        except fastpg.MissingColumnError as e:
            print("error ->", e)
