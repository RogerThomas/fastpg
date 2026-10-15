# 0041. Open-loop API traffic benchmark

- Status: Accepted
- Date: 2026-10-10

## Context

Closed-loop saturation benchmarks hide queueing and tail latency, which is what API
services experience.

## Decision

Add `bench/open_loop.py` alongside the closed-loop scenario:

- Poisson arrivals at fixed rates, a pool of 20 connections per driver;
- latency split into service latency (dispatch to result) and dispatch lag (scheduled to
  dispatched), so uvloop's ~1 ms timer granularity is not charged to drivers but a
  loop-hogging driver still shows up.

## Consequences

The "loop busy" column is only a fair driver comparison at low rates: above ~1k/s the
generator busy-waits to hit sub-ms arrival times, inflating it equally for every driver.
