//! Authentication for `channel_binding=disable` and `require`.
//!
//! xitca 0.4 picks the SASL mechanism itself and has no setting for it: SCRAM-SHA-256-PLUS
//! whenever the server offers it and `Config::tls_server_end_point` is set, otherwise
//! SCRAM-SHA-256 with the gs2 flag `y`. So through xitca a login can't be required to be
//! channel-bound (`require`: xitca also answers password and md5 requests, and accepts an
//! `AuthenticationOk` without any SCRAM), and SCRAM over TLS can't be unbound (`disable`:
//! a server that offers SCRAM-SHA-256-PLUS rejects `y` as a downgrade, it accepts only `n`).
//!
//! For these two values [`authenticate`] runs the exchange before xitca starts: it sends the
//! startup message xitca would send and answers the server's authentication requests as
//! libpq does, with `postgres-protocol`'s encoders and SCRAM client (the ones xitca uses), up
//! to `AuthenticationOk`. xitca then starts over [`Authenticated`], which drops xitca's own
//! startup message and replays the server's messages from `AuthenticationOk` on, so xitca
//! reads them as the answer to its startup. An `ErrorResponse` (a wrong password, an unknown
//! database) is replayed the same way, so xitca reports it as usual. `prefer`, the default,
//! leaves authentication to xitca.

use std::io::{self, Read, Write};
use std::pin::Pin;
use std::task::{Context, Poll};

use bytes::{Buf, BytesMut};
use fallible_iterator::FallibleIterator;
use postgres_protocol::authentication::md5_hash;
use postgres_protocol::authentication::sasl::{
    self, SCRAM_SHA_256, SCRAM_SHA_256_PLUS, ScramSha256,
};
use postgres_protocol::message::backend::Message;
use postgres_protocol::message::frontend;
use xitca_io::io::{AsyncIo, Interest, Ready};
use xitca_postgres::Config;

use crate::errors::QueryError;
use crate::tls::{ChannelBinding, write_all};

fn interface(message: impl Into<String>) -> QueryError {
    QueryError::Interface(message.into())
}

fn failed(e: io::Error) -> QueryError {
    interface(format!("authentication failed: {e}"))
}

/// Why `require` refuses a login, in libpq's words.
fn unbound(what: &str) -> QueryError {
    interface(format!("channel binding required, but {what}"))
}

/// The SASL mechanism to answer `offered` with under `binding` (`Disable` or `Require`), and
/// the channel binding it uses.
fn choose(
    binding: ChannelBinding,
    offered: &[&str],
    end_point: Option<&[u8]>,
) -> Result<(&'static str, sasl::ChannelBinding), QueryError> {
    if binding == ChannelBinding::Require {
        if !offered.contains(&SCRAM_SHA_256_PLUS) {
            return Err(unbound("the server did not offer SCRAM-SHA-256-PLUS"));
        }
        // Ed25519 and Ed448 certificates have no `tls-server-end-point` hash (`tls.rs`).
        let Some(end_point) = end_point else {
            return Err(unbound(
                "the server certificate's signature algorithm has no tls-server-end-point hash",
            ));
        };
        let data = sasl::ChannelBinding::tls_server_end_point(end_point.to_vec());
        return Ok((SCRAM_SHA_256_PLUS, data));
    }
    if !offered.contains(&SCRAM_SHA_256) {
        return Err(interface(format!(
            "the server offered no supported SASL mechanism ({}), and \
             channel_binding=disable allows only SCRAM-SHA-256",
            offered.join(", ")
        )));
    }
    // gs2 flag `n`: no channel binding, accepted with or without TLS.
    Ok((SCRAM_SHA_256, sasl::ChannelBinding::unsupported()))
}

/// The length of the message at the start of `buf` (tag included), once its header is in.
fn message_len(buf: &[u8]) -> Result<Option<usize>, QueryError> {
    let Some(header) = buf.get(1..).and_then(<[u8]>::first_chunk::<4>) else {
        return Ok(None);
    };
    let len = u32::from_be_bytes(*header);
    if len < 4 {
        return Err(interface(format!(
            "invalid message length {len} during authentication"
        )));
    }
    Ok(Some(len as usize + 1))
}

/// The next complete message from the server, read into `buf` as needed.
async fn next_message<Io: AsyncIo>(
    io: &mut Io,
    buf: &mut BytesMut,
) -> Result<BytesMut, QueryError> {
    let mut chunk = [0u8; 4096];
    loop {
        if let Some(len) = message_len(buf)?
            && buf.len() >= len
        {
            return Ok(buf.split_to(len));
        }
        match io.read(&mut chunk) {
            Ok(0) => {
                return Err(interface(
                    "server closed the connection during authentication",
                ));
            }
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                io.ready(Interest::READABLE).await.map_err(failed)?;
            }
            Err(e) => return Err(failed(e)),
        }
    }
}

/// What xitca reads first: the server's messages from `message` on.
fn replay(mut message: BytesMut, rest: &[u8]) -> BytesMut {
    message.extend_from_slice(rest);
    message
}

async fn send<Io: AsyncIo>(io: &mut Io, out: &mut BytesMut) -> Result<(), QueryError> {
    write_all(io, out).await.map_err(failed)?;
    out.clear();
    Ok(())
}

/// Authenticate on the freshly connected (and, if `encrypted`, TLS) `io` as `binding` asks,
/// for the user in `cfg`. Returns the stream to hand to xitca.
pub async fn authenticate<Io: AsyncIo>(
    mut io: Io,
    cfg: &Config,
    binding: ChannelBinding,
    encrypted: bool,
) -> Result<Authenticated<Io>, QueryError> {
    let require = binding == ChannelBinding::Require;
    if require && !encrypted {
        return Err(interface(
            "channel_binding=require needs TLS, but the connection is not encrypted (the server \
             declined TLS, or it is a unix socket)",
        ));
    }
    // What xitca's startup message holds (`session.rs`), in the same order.
    let mut params = vec![("client_encoding", "UTF8")];
    let named = [
        ("user", cfg.get_user()),
        ("database", cfg.get_dbname()),
        ("options", cfg.get_options()),
        ("application_name", cfg.get_application_name()),
    ];
    params.extend(named.into_iter().filter_map(|(k, v)| Some((k, v?))));
    let mut out = BytesMut::new();
    frontend::startup_message(params, &mut out).map_err(failed)?;
    send(&mut io, &mut out).await?;

    let password = || {
        cfg.get_password()
            .ok_or_else(|| interface("password is missing"))
    };
    let mut buf = BytesMut::new();
    let mut scram: Option<ScramSha256> = None;
    // The server proved it knows the password (and, for `require`, the TLS session).
    let mut verified = false;
    loop {
        let raw = next_message(&mut io, &mut buf).await?;
        let message = Message::parse(&mut raw.clone())
            .map_err(failed)?
            .ok_or_else(|| interface("incomplete message during authentication"))?;
        match message {
            Message::AuthenticationOk => {
                // E.g. a `trust` login: the server never proved it knows the password.
                if require && !verified {
                    return Err(unbound(
                        "the server authenticated the client without channel binding",
                    ));
                }
                if scram.is_some() && !verified {
                    return Err(interface(
                        "the server sent AuthenticationOk before the SCRAM exchange finished",
                    ));
                }
                return Ok(Authenticated::new(io, replay(raw, &buf)));
            }
            Message::ErrorResponse(_) => return Ok(Authenticated::new(io, replay(raw, &buf))),
            // `require` never sends a password the server could read or replay.
            Message::AuthenticationCleartextPassword if require => {
                return Err(unbound("the server requested password authentication"));
            }
            Message::AuthenticationMd5Password(_) if require => {
                return Err(unbound("the server requested md5 authentication"));
            }
            Message::AuthenticationCleartextPassword => {
                frontend::password_message(password()?, &mut out).map_err(failed)?;
                send(&mut io, &mut out).await?;
            }
            Message::AuthenticationMd5Password(body) => {
                let user = cfg
                    .get_user()
                    .ok_or_else(|| interface("username is missing"))?;
                let hash = md5_hash(user.as_bytes(), password()?, body.salt());
                frontend::password_message(hash.as_bytes(), &mut out).map_err(failed)?;
                send(&mut io, &mut out).await?;
            }
            Message::AuthenticationSasl(body) => {
                let offered: Vec<&str> = body.mechanisms().collect().map_err(failed)?;
                let (mechanism, data) = choose(binding, &offered, cfg.get_tls_server_end_point())?;
                let client = ScramSha256::new(password()?, data);
                frontend::sasl_initial_response(mechanism, client.message(), &mut out)
                    .map_err(failed)?;
                scram = Some(client);
                send(&mut io, &mut out).await?;
            }
            Message::AuthenticationSaslContinue(body) => {
                let client = scram
                    .as_mut()
                    .ok_or_else(|| interface("unexpected SASL message from the server"))?;
                client.update(body.data()).map_err(failed)?;
                frontend::sasl_response(client.message(), &mut out).map_err(failed)?;
                send(&mut io, &mut out).await?;
            }
            Message::AuthenticationSaslFinal(body) => {
                let client = scram
                    .as_mut()
                    .ok_or_else(|| interface("unexpected SASL message from the server"))?;
                // Checks the server's signature.
                client.finish(body.data()).map_err(failed)?;
                verified = true;
            }
            Message::AuthenticationKerberosV5
            | Message::AuthenticationScmCredential
            | Message::AuthenticationGss
            | Message::AuthenticationGssContinue(_)
            | Message::AuthenticationSspi => {
                return Err(interface(
                    "the server requested an authentication method fastpg does not support",
                ));
            }
            // Notices, as in xitca.
            _ => {}
        }
    }
}

/// xitca's stream after [`authenticate`]: drops the startup message xitca writes first (the
/// server has had one) and reads the server's messages that [`authenticate`] read past first.
pub struct Authenticated<Io> {
    io: Io,
    replay: BytesMut,
    /// The startup message's length field, as far as written (it counts itself).
    header: [u8; 4],
    header_len: usize,
    /// Bytes of the startup message still to drop after the length field.
    remaining: usize,
}

impl<Io> Authenticated<Io> {
    fn new(io: Io, replay: BytesMut) -> Self {
        Self {
            io,
            replay,
            header: [0; 4],
            header_len: 0,
            remaining: 0,
        }
    }

    /// The startup message has been dropped.
    fn started(&self) -> bool {
        self.header_len == self.header.len() && self.remaining == 0
    }

    /// Drop the start of `buf` that belongs to the startup message; how much that was (at
    /// least one byte of a non-empty `buf`, until [`started`](Self::started)).
    fn drop_startup(&mut self, buf: &[u8]) -> usize {
        let mut used = 0;
        if self.header_len < self.header.len() {
            used = (self.header.len() - self.header_len).min(buf.len());
            self.header[self.header_len..self.header_len + used].copy_from_slice(&buf[..used]);
            self.header_len += used;
            if self.header_len < self.header.len() {
                return used;
            }
            self.remaining = (u32::from_be_bytes(self.header) as usize).saturating_sub(4);
        }
        let take = self.remaining.min(buf.len() - used);
        self.remaining -= take;
        used + take
    }
}

impl<Io: Read> Read for Authenticated<Io> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.replay.is_empty() {
            return self.io.read(buf);
        }
        let n = self.replay.len().min(buf.len());
        buf[..n].copy_from_slice(&self.replay[..n]);
        self.replay.advance(n);
        Ok(n)
    }
}

impl<Io: Write> Write for Authenticated<Io> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        if self.started() || buf.is_empty() {
            return self.io.write(buf);
        }
        Ok(self.drop_startup(buf))
    }

    fn write_vectored(&mut self, bufs: &[io::IoSlice<'_>]) -> io::Result<usize> {
        if self.started() {
            return self.io.write_vectored(bufs);
        }
        let first = bufs
            .iter()
            .find(|b| !b.is_empty())
            .map_or(&[][..], |b| &**b);
        self.write(first)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.io.flush()
    }
}

impl<Io: AsyncIo + Send> AsyncIo for Authenticated<Io> {
    async fn ready(&mut self, interest: Interest) -> io::Result<Ready> {
        if interest.is_readable() && !self.replay.is_empty() {
            return Ok(Ready::READABLE);
        }
        self.io.ready(interest).await
    }

    fn poll_ready(&mut self, interest: Interest, cx: &mut Context<'_>) -> Poll<io::Result<Ready>> {
        if interest.is_readable() && !self.replay.is_empty() {
            return Poll::Ready(Ok(Ready::READABLE));
        }
        self.io.poll_ready(interest, cx)
    }

    fn is_vectored_write(&self) -> bool {
        self.io.is_vectored_write()
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().io).poll_shutdown(cx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// An in-memory stream: reads `input`, collects what is written.
    #[derive(Default)]
    struct Memory {
        input: io::Cursor<Vec<u8>>,
        written: Vec<u8>,
    }

    impl Read for Memory {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            self.input.read(buf)
        }
    }

    impl Write for Memory {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            self.written.extend_from_slice(buf);
            Ok(buf.len())
        }

        fn write_vectored(&mut self, bufs: &[io::IoSlice<'_>]) -> io::Result<usize> {
            for buf in bufs {
                self.written.extend_from_slice(buf);
            }
            Ok(bufs.iter().map(|b| b.len()).sum())
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn startup() -> Vec<u8> {
        let mut out = BytesMut::new();
        frontend::startup_message([("user", "user")], &mut out).expect("encodes");
        out.to_vec()
    }

    fn error(result: Result<(&'static str, sasl::ChannelBinding), QueryError>) -> String {
        match result {
            Err(QueryError::Interface(m)) => m,
            Ok((mechanism, _)) => panic!("expected an error, got {mechanism}"),
            Err(_) => panic!("unexpected error"),
        }
    }

    #[test]
    fn startup_is_dropped_however_it_is_written() {
        let query = b"Q\0\0\0\x0dSELECT 1\0";
        let message = startup();
        for piece in [1, 2, 3, 5, 4096] {
            let mut io = Authenticated::new(Memory::default(), BytesMut::new());
            let stream = [&message[..], query].concat();
            let mut rest = &stream[..];
            while !rest.is_empty() {
                let n = io.write(&rest[..piece.min(rest.len())]).expect("writes");
                assert!(n > 0, "piece {piece}");
                rest = &rest[n..];
            }
            assert_eq!(io.io.written, query, "piece {piece}");
        }
    }

    #[test]
    fn vectored_writes_drop_the_startup_too() {
        let message = startup();
        let mut io = Authenticated::new(Memory::default(), BytesMut::new());
        let bufs = [io::IoSlice::new(&[]), io::IoSlice::new(&message)];
        assert_eq!(io.write_vectored(&bufs).expect("writes"), message.len());
        let bufs = [io::IoSlice::new(b"ab"), io::IoSlice::new(b"c")];
        assert_eq!(io.write_vectored(&bufs).expect("writes"), 3);
        assert_eq!(io.io.written, b"abc");
    }

    #[test]
    fn replay_is_read_before_the_stream() {
        let memory = Memory {
            input: io::Cursor::new(b"cd".to_vec()),
            written: Vec::new(),
        };
        let mut io = Authenticated::new(memory, BytesMut::from(&b"ab"[..]));
        let mut buf = [0u8; 1];
        let mut read = Vec::new();
        while io.read(&mut buf).expect("reads") == 1 {
            read.push(buf[0]);
        }
        assert_eq!(read, b"abcd");
    }

    #[test]
    fn messages_are_framed_by_their_length() {
        assert_eq!(message_len(b"R\0\0").ok().flatten(), None);
        assert_eq!(message_len(b"R\0\0\0\x08").ok().flatten(), Some(9));
        assert!(message_len(b"R\0\0\0\x03").is_err());
    }

    #[test]
    fn require_takes_only_scram_plus_with_an_end_point_hash() {
        let both = [SCRAM_SHA_256_PLUS, SCRAM_SHA_256];
        let hash = Some(&b"hash"[..]);
        let chosen = choose(ChannelBinding::Require, &both, hash).map(|(m, _)| m);
        assert!(matches!(chosen, Ok(SCRAM_SHA_256_PLUS)));
        let message = error(choose(ChannelBinding::Require, &[SCRAM_SHA_256], hash));
        assert!(
            message.contains("did not offer SCRAM-SHA-256-PLUS"),
            "{message}"
        );
        let message = error(choose(ChannelBinding::Require, &both, None));
        assert!(
            message.contains("no tls-server-end-point hash"),
            "{message}"
        );
    }

    #[test]
    fn disable_takes_plain_scram_even_when_plus_is_offered() {
        let both = [SCRAM_SHA_256_PLUS, SCRAM_SHA_256];
        let chosen = choose(ChannelBinding::Disable, &both, Some(b"hash")).map(|(m, _)| m);
        assert!(matches!(chosen, Ok(SCRAM_SHA_256)));
        // The gs2 header `n,,`: no channel binding.
        let (_, data) = choose(ChannelBinding::Disable, &[SCRAM_SHA_256], None)
            .unwrap_or_else(|_| panic!("SCRAM-SHA-256 is offered"));
        let client = ScramSha256::new(b"password", data);
        assert!(client.message().starts_with(b"n,,"));
        let message = error(choose(ChannelBinding::Disable, &[SCRAM_SHA_256_PLUS], None));
        assert!(message.contains("allows only SCRAM-SHA-256"), "{message}");
    }
}
