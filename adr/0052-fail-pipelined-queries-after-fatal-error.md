# 0052. Fail queries pipelined behind a fatal error

- Status: Accepted
- Date: 2026-10-10

## Context

Queries pipelined behind a FATAL error, or sent just after one, could wait forever: the
dropped xitca driver left response senders alive.

## Decision

Every native operation races a per-connection "dead" flag. The bulk-write tasks get the same
protection, and their Python feeder races chunk acknowledgements against completion.

## Consequences

Fatal errors fail every pending operation promptly. A COPY interrupted by
`pg_terminate_backend` fails promptly (tested).
