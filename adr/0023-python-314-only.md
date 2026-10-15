# 0023. Python 3.14 only, Rust edition 2024

- Status: Accepted
- Date: 2026-10-10

## Context

The project targets current toolchains and uses Python 3.14 features such as t-strings.

## Decision

Require Python 3.14 (`requires-python`) and use Rust edition 2024.

## Consequences

No compatibility code for older interpreters. Users on earlier Python versions cannot use
fastpg.
