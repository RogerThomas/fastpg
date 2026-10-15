# 0037. json strategy for msgspec Structs

- Status: Accepted
- Date: 2026-10-10

## Context

The JSON encoder built for pydantic ([0035](0035-pydantic-json-and-direct-routes.md)) also works for msgspec.

## Decision

`strategy="json"` is available for msgspec Structs via `msgspec.json.Decoder`.

## Consequences

An extra comparison point next to msgpack, at no extra encoder code.
