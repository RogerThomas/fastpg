# 0055. Treat fatal socket errors as end-of-stream

- Status: Accepted
- Date: 2026-10-10

## Context

After a server FATAL on Linux, the next write succeeds, the server answers with a TCP RST,
and the next read fails with `ECONNRESET` instead of a clean 0-byte EOF. The socket wrapper
from [0026](0026-detect-server-closed-connections.md) only signalled on 0-byte reads, and xitca waits for another client write after
a read error, so the connection hung. macOS happened to deliver the clean EOF first.

## Decision

Any fatal read or write error (anything but `WouldBlock`/`Interrupted`) also signals
end-of-stream.

## Consequences

Dead connections are detected on both platforms; verified 3/3 on Linux and macOS. The Docker
test run ([0025](0025-docker-test-runner.md)) caught this.
