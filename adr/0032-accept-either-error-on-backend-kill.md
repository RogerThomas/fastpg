# 0032. Accept either error after a backend kill

- Status: Accepted
- Date: 2026-10-10

## Context

When the server kills a backend, the next query sees either the server's `57P01` error or
the closed socket, depending on timing. A test expecting one of them was flaky by design.

## Decision

The test accepts either outcome.

## Consequences

The test is deterministic; both outcomes are valid behaviour.
