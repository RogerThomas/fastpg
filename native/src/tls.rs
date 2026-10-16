//! TLS (libpq's `sslmode` and friends).
//!
//! fastpg connects the socket itself and hands it to xitca via `Postgres::connect_io`, which
//! skips xitca's own TLS negotiation (and xitca's `tls` feature only offers an unverified
//! `require`, see `notes/tls.md`). So TLS happens here, between the socket and xitca:
//! [`start`] sends the PostgreSQL `SSLRequest` (encoded by `postgres-protocol`, as xitca
//! does) or, for `sslnegotiation=direct`, starts the handshake at once; then it runs the
//! rustls handshake over the socket's [`AsyncIo`] and returns a [`TlsStream`]: an `AsyncIo`
//! that encrypts and decrypts on the fly. rustls is sans-I/O, so it runs over the loop-driven
//! `LoopSocket` (`event_loop.rs`) like over any other `AsyncIo`.
//!
//! `net.rs` wraps the result in `EofWatch` as before, so a TLS `close_notify`, a TCP EOF
//! without one, a TLS error and a socket error all end the connection the same way.
//!
//! The DSN's TLS keys (and libpq's `connect_timeout`) are taken out here (xitca rejects keys
//! and `sslmode` values it does not know); the rest of the DSN goes to xitca unchanged.

use std::borrow::Cow;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::{Arc, OnceLock};
use std::task::{Context, Poll};

use bytes::BytesMut;
use percent_encoding::percent_decode_str;
use postgres_protocol::message::frontend;
use rustls::client::WebPkiServerVerifier;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::client::verify_server_cert_signed_by_trust_anchor;
use rustls::crypto::{
    CryptoProvider, WebPkiSupportedAlgorithms, verify_tls12_signature, verify_tls13_signature,
};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName, UnixTime};
use rustls::server::ParsedCertificate;
use rustls::{
    CertificateError, ClientConfig, ClientConnection, DigitallySignedStruct, RootCertStore,
    SignatureScheme,
};
use sha2::{Digest, Sha224, Sha256, Sha384, Sha512};
use xitca_io::io::{AsyncIo, Interest, Ready};
use xitca_postgres::Config;

use crate::errors::QueryError;

/// ALPN protocol name PostgreSQL 17+ expects (required for `sslnegotiation=direct`).
const ALPN: &[u8] = b"postgresql";

fn interface(message: impl Into<String>) -> QueryError {
    QueryError::Interface(message.into())
}

/// libpq's `sslmode`, in increasing order of strictness (`allow` is not supported).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum SslMode {
    Disable,
    Prefer,
    Require,
    VerifyCa,
    VerifyFull,
}

impl SslMode {
    fn parse(value: &str) -> Result<Self, QueryError> {
        Ok(match value {
            "disable" => Self::Disable,
            "prefer" => Self::Prefer,
            "require" => Self::Require,
            "verify-ca" => Self::VerifyCa,
            "verify-full" => Self::VerifyFull,
            "allow" => {
                return Err(interface(
                    "sslmode=allow is not supported (use \"disable\" or \"prefer\")",
                ));
            }
            other => return Err(interface(format!("invalid sslmode value: {other:?}"))),
        })
    }

    fn name(self) -> &'static str {
        match self {
            Self::Disable => "disable",
            Self::Prefer => "prefer",
            Self::Require => "require",
            Self::VerifyCa => "verify-ca",
            Self::VerifyFull => "verify-full",
        }
    }
}

/// libpq's `channel_binding`: whether SCRAM authentication is bound to the TLS session.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChannelBinding {
    /// Plain SCRAM-SHA-256, even when the server offers SCRAM-SHA-256-PLUS.
    Disable,
    /// SCRAM-SHA-256-PLUS when the connection uses TLS and the server offers it.
    Prefer,
    /// SCRAM-SHA-256-PLUS over TLS, or the connection fails.
    Require,
}

impl ChannelBinding {
    fn parse(value: &str) -> Result<Self, QueryError> {
        Ok(match value {
            "disable" => Self::Disable,
            "prefer" => Self::Prefer,
            "require" => Self::Require,
            other => {
                return Err(interface(format!(
                    "invalid channel_binding value: {other:?} (use \"disable\", \"prefer\" or \
                     \"require\")"
                )));
            }
        })
    }
}

/// Where trusted root certificates come from.
#[derive(Clone, Debug, PartialEq, Eq)]
enum RootCert {
    /// `sslrootcert=system` (or a `verify-*` mode without `sslrootcert`).
    System,
    File(PathBuf),
}

/// The TLS settings of a DSN.
#[derive(Clone, Debug)]
pub struct TlsOptions {
    mode: SslMode,
    /// `sslnegotiation=direct`: TLS handshake right away, no `SSLRequest` (PostgreSQL 17+).
    direct: bool,
    /// `sslrootcert`, if given.
    root_cert: Option<RootCert>,
    /// `sslsni`: send the host name in the handshake (DNS names only).
    sni: bool,
    /// `sslcert` and `sslkey`.
    client_cert: Option<(PathBuf, PathBuf)>,
    channel_binding: ChannelBinding,
}

impl TlsOptions {
    pub fn channel_binding(&self) -> ChannelBinding {
        self.channel_binding
    }

    fn roots(&self) -> Result<Arc<RootCertStore>, QueryError> {
        match &self.root_cert {
            Some(RootCert::File(path)) if !path.exists() => Err(interface(format!(
                "root certificate file \"{}\" does not exist (set sslrootcert to the CA \
                 certificate, use sslrootcert=system for the system's trusted roots, or \
                 sslmode=require to skip certificate verification)",
                path.display()
            ))),
            Some(RootCert::File(path)) => load_roots(path).map(Arc::new),
            Some(RootCert::System) | None => Ok(system_roots()),
        }
    }

    /// How the server certificate is checked. Like libpq, `prefer`/`require` verify the
    /// chain (not the name) when an `sslrootcert` file exists, and nothing otherwise.
    fn verifier(
        &self,
        provider: &Arc<CryptoProvider>,
    ) -> Result<Arc<dyn ServerCertVerifier>, QueryError> {
        let algs = provider.signature_verification_algorithms;
        let roots = match (self.mode, &self.root_cert) {
            (SslMode::VerifyFull, _) => {
                return WebPkiServerVerifier::builder_with_provider(
                    self.roots()?,
                    provider.clone(),
                )
                .build()
                .map(|v| v as Arc<dyn ServerCertVerifier>)
                .map_err(|e| interface(format!("TLS setup failed: {e}")));
            }
            (SslMode::VerifyCa, _) => Some(self.roots()?),
            (_, Some(RootCert::File(path))) if path.exists() => Some(self.roots()?),
            _ => None,
        };
        Ok(Arc::new(Verifier { roots, algs }))
    }

    fn client_config(&self) -> Result<Arc<ClientConfig>, QueryError> {
        let provider = provider();
        let builder = ClientConfig::builder_with_provider(provider.clone())
            .with_safe_default_protocol_versions()
            .map_err(|e| interface(format!("TLS setup failed: {e}")))?
            .dangerous()
            .with_custom_certificate_verifier(self.verifier(&provider)?);
        let mut config = match &self.client_cert {
            None => builder.with_no_client_auth(),
            Some((cert, key)) => builder
                .with_client_auth_cert(load_certs(cert, "client certificate")?, load_key(key)?)
                .map_err(|e| interface(format!("invalid client certificate or key: {e}")))?,
        };
        // libpq 17+ always offers ALPN; servers before 17 ignore it.
        config.alpn_protocols = vec![ALPN.to_vec()];
        config.enable_sni = self.sni;
        Ok(Arc::new(config))
    }
}

/// The ring provider (no aws-lc build dependency), shared by every connection.
fn provider() -> Arc<CryptoProvider> {
    static PROVIDER: OnceLock<Arc<CryptoProvider>> = OnceLock::new();
    PROVIDER
        .get_or_init(|| Arc::new(rustls::crypto::ring::default_provider()))
        .clone()
}

/// The operating system's trusted roots (`SSL_CERT_FILE`/`SSL_CERT_DIR` honoured), loaded
/// once per process; Mozilla's bundled roots when the system has none (e.g. a scratch image).
fn system_roots() -> Arc<RootCertStore> {
    static ROOTS: OnceLock<Arc<RootCertStore>> = OnceLock::new();
    ROOTS
        .get_or_init(|| {
            let mut store = RootCertStore::empty();
            store.add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
            if store.is_empty() {
                store.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
            }
            Arc::new(store)
        })
        .clone()
}

fn load_certs(path: &Path, what: &str) -> Result<Vec<CertificateDer<'static>>, QueryError> {
    let unreadable = |e: &dyn std::fmt::Display| {
        interface(format!("could not read {what} \"{}\": {e}", path.display()))
    };
    let certs = CertificateDer::pem_file_iter(path)
        .map_err(|e| unreadable(&e))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| unreadable(&e))?;
    if certs.is_empty() {
        return Err(unreadable(&"no PEM certificates found"));
    }
    Ok(certs)
}

fn load_roots(path: &Path) -> Result<RootCertStore, QueryError> {
    let mut store = RootCertStore::empty();
    for cert in load_certs(path, "root certificate file")? {
        store.add(cert).map_err(|e| {
            interface(format!(
                "invalid root certificate in \"{}\": {e}",
                path.display()
            ))
        })?;
    }
    Ok(store)
}

fn load_key(path: &Path) -> Result<PrivateKeyDer<'static>, QueryError> {
    PrivateKeyDer::from_pem_file(path).map_err(|e| {
        interface(format!(
            "could not read client key \"{}\" (encrypted keys are not supported): {e}",
            path.display()
        ))
    })
}

/// Server certificate check for everything but `verify-full`: the chain must lead to `roots`
/// (`verify-ca`), or, without roots, any certificate is accepted (`prefer`/`require`). The
/// handshake signature is always verified, so the server proves it holds the certificate's
/// key (channel binding relies on that).
#[derive(Debug)]
struct Verifier {
    roots: Option<Arc<RootCertStore>>,
    algs: WebPkiSupportedAlgorithms,
}

impl ServerCertVerifier for Verifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        if let Some(roots) = &self.roots {
            let cert = ParsedCertificate::try_from(end_entity)?;
            verify_server_cert_signed_by_trust_anchor(
                &cert,
                roots,
                intermediates,
                now,
                self.algs.all,
            )?;
        }
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(message, cert, dss, &self.algs)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(message, cert, dss, &self.algs)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.algs.supported_schemes()
    }
}

// --- DSN -------------------------------------------------------------------------------

/// The DSN keys xitca's parser accepts besides `sslmode` and `sslnegotiation` (taken here).
const XITCA_KEYS: &[&str] = &[
    "user",
    "password",
    "dbname",
    "options",
    "application_name",
    "host",
    "port",
    "target_session_attrs",
];

/// TLS keys and `connect_timeout` found in a DSN (last one wins, as in xitca's parser).
#[derive(Default)]
struct Raw {
    connect_timeout: Option<String>,
    sslmode: Option<String>,
    sslnegotiation: Option<String>,
    sslrootcert: Option<String>,
    sslsni: Option<String>,
    channel_binding: Option<String>,
    sslcert: Option<String>,
    sslkey: Option<String>,
    sslpassword: Option<String>,
    /// The first key neither fastpg nor xitca knows (xitca's own error for it says nothing).
    unknown: Option<String>,
}

impl Raw {
    /// Keep `value` if `key` is a TLS key or `connect_timeout`; `false` otherwise.
    fn take(&mut self, key: &str, value: String) -> bool {
        let slot = match key {
            "connect_timeout" => &mut self.connect_timeout,
            "sslmode" => &mut self.sslmode,
            "sslnegotiation" => &mut self.sslnegotiation,
            "sslrootcert" => &mut self.sslrootcert,
            "sslsni" => &mut self.sslsni,
            "channel_binding" => &mut self.channel_binding,
            "sslcert" => &mut self.sslcert,
            "sslkey" => &mut self.sslkey,
            "sslpassword" => &mut self.sslpassword,
            _ => {
                if self.unknown.is_none() && !XITCA_KEYS.contains(&key) {
                    self.unknown = Some(key.to_owned());
                }
                return false;
            }
        };
        *slot = Some(value);
        true
    }

    fn into_options(self) -> Result<TlsOptions, QueryError> {
        if let Some(key) = self.unknown {
            return Err(interface(format!("unsupported connection option {key:?}")));
        }
        if self.sslpassword.is_some() {
            return Err(interface(
                "sslpassword is not supported (encrypted client keys); use an unencrypted key",
            ));
        }
        let root_cert = self.sslrootcert.map(|v| match v.as_str() {
            "system" => RootCert::System,
            _ => RootCert::File(PathBuf::from(v)),
        });
        let system = root_cert == Some(RootCert::System);
        let mode = match self.sslmode.as_deref() {
            Some(value) => SslMode::parse(value)?,
            // libpq: sslrootcert=system makes verify-full the default.
            None if system => SslMode::VerifyFull,
            None => SslMode::Prefer,
        };
        if system && mode != SslMode::VerifyFull {
            return Err(interface(format!(
                "weak sslmode \"{}\" may not be used with sslrootcert=system (use \"verify-full\")",
                mode.name()
            )));
        }
        let direct = match self.sslnegotiation.as_deref() {
            None | Some("postgres") => false,
            Some("direct") => true,
            Some(other) => {
                return Err(interface(format!(
                    "invalid sslnegotiation value: {other:?}"
                )));
            }
        };
        if direct && mode < SslMode::Require {
            return Err(interface(format!(
                "weak sslmode \"{}\" may not be used with sslnegotiation=direct (use \"require\", \
                 \"verify-ca\", or \"verify-full\")",
                mode.name()
            )));
        }
        let sni = match self.sslsni.as_deref() {
            None | Some("1") => true,
            Some("0") => false,
            Some(other) => return Err(interface(format!("invalid sslsni value: {other:?}"))),
        };
        let channel_binding = match self.channel_binding.as_deref() {
            Some(value) => ChannelBinding::parse(value)?,
            None => ChannelBinding::Prefer,
        };
        if channel_binding == ChannelBinding::Require && mode == SslMode::Disable {
            return Err(interface(
                "channel_binding=require needs TLS, but sslmode is \"disable\"",
            ));
        }
        let client_cert = match (self.sslcert, self.sslkey) {
            (None, None) => None,
            (Some(cert), Some(key)) => Some((PathBuf::from(cert), PathBuf::from(key))),
            _ => return Err(interface("sslcert and sslkey must be given together")),
        };
        Ok(TlsOptions {
            mode,
            direct,
            root_cert,
            sni,
            client_cert,
            channel_binding,
        })
    }
}

/// The DSN keys fastpg handles itself (xitca never sees them).
pub struct DsnOptions {
    pub tls: TlsOptions,
    /// libpq's `connect_timeout`, in seconds (applied in Python, per address tried).
    pub connect_timeout: Option<u32>,
}

/// libpq's `connect_timeout`: whole seconds; zero, negative or absent mean no timeout, and
/// 1 means 2 (libpq's minimum).
fn connect_timeout(value: Option<&str>) -> Result<Option<u32>, QueryError> {
    let Some(value) = value else {
        return Ok(None);
    };
    let seconds: i64 = value.trim().parse().map_err(|_| {
        interface(format!(
            "invalid integer value {value:?} for connection option \"connect_timeout\""
        ))
    })?;
    Ok(match seconds {
        ..=0 => None,
        1 => Some(2),
        n => Some(u32::try_from(n).unwrap_or(u32::MAX)),
    })
}

/// Split the TLS keys and `connect_timeout` off `dsn`: the DSN for xitca (unchanged if it had
/// none) and their settings. Both forms are handled, URL (`postgresql://...?sslmode=require`)
/// and key/value (`host=db sslmode=require`), each split the way xitca's own parser reads it.
pub fn split_dsn(dsn: &str) -> Result<(Cow<'_, str>, DsnOptions), QueryError> {
    let mut raw = Raw::default();
    let rest = match ["postgresql://", "postgres://"]
        .iter()
        .find(|prefix| dsn.starts_with(**prefix))
    {
        Some(prefix) => strip_url(dsn, prefix.len(), &mut raw),
        None => strip_key_values(dsn, &mut raw),
    };
    let connect_timeout = connect_timeout(raw.connect_timeout.take().as_deref())?;
    let tls = raw.into_options()?;
    Ok((
        rest,
        DsnOptions {
            tls,
            connect_timeout,
        },
    ))
}

fn strip_url<'a>(dsn: &'a str, body: usize, raw: &mut Raw) -> Cow<'a, str> {
    // As in xitca: the credentials end at the first '@', the parameters start at the next '?'.
    let rest = &dsn[body..];
    let host_at = rest.find('@').map_or(0, |i| i + 1);
    let Some(query) = rest[host_at..].find('?').map(|i| body + host_at + i) else {
        return Cow::Borrowed(dsn);
    };
    let mut kept = Vec::new();
    let mut taken = false;
    for pair in dsn[query + 1..].split('&').filter(|p| !p.is_empty()) {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        let key = percent_decode_str(key).decode_utf8_lossy();
        let value = percent_decode_str(value).decode_utf8_lossy().into_owned();
        if raw.take(&key, value) {
            taken = true;
        } else {
            kept.push(pair);
        }
    }
    if !taken {
        return Cow::Borrowed(dsn);
    }
    let mut out = dsn[..query].to_owned();
    if !kept.is_empty() {
        out.push('?');
        out.push_str(&kept.join("&"));
    }
    Cow::Owned(out)
}

fn strip_key_values<'a>(dsn: &'a str, raw: &mut Raw) -> Cow<'a, str> {
    let mut scan = KeyValues { s: dsn, pos: 0 };
    let mut kept = Vec::new();
    let mut taken = false;
    loop {
        match scan.next() {
            Step::Param { text, key, value } => {
                if raw.take(key, value) {
                    taken = true;
                } else {
                    kept.push(text);
                }
            }
            // xitca stops at the same point and ignores what follows: keep it verbatim.
            Step::End => break,
            // Malformed: leave the whole DSN to xitca, which reports it.
            Step::Malformed => return Cow::Borrowed(dsn),
        }
    }
    if !taken {
        return Cow::Borrowed(dsn);
    }
    let tail = dsn[scan.pos..].trim();
    if !tail.is_empty() {
        kept.push(tail);
    }
    Cow::Owned(kept.join(" "))
}

enum Step<'a> {
    Param {
        /// The whole `key = value` text, as written.
        text: &'a str,
        key: &'a str,
        value: String,
    },
    End,
    Malformed,
}

/// A scanner for key/value DSNs with xitca's (libpq's) grammar: `key = value` pairs
/// separated by whitespace; values are bare or single-quoted, with backslash escapes.
struct KeyValues<'a> {
    s: &'a str,
    pos: usize,
}

impl<'a> KeyValues<'a> {
    fn peek(&self) -> Option<char> {
        self.s[self.pos..].chars().next()
    }

    fn bump(&mut self) -> Option<char> {
        let c = self.peek()?;
        self.pos += c.len_utf8();
        Some(c)
    }

    fn skip_whitespace(&mut self) {
        while self.peek().is_some_and(char::is_whitespace) {
            self.bump();
        }
    }

    fn next(&mut self) -> Step<'a> {
        self.skip_whitespace();
        let start = self.pos;
        while self.peek().is_some_and(|c| !c.is_whitespace() && c != '=') {
            self.bump();
        }
        if self.pos == start {
            return Step::End;
        }
        let key = &self.s[start..self.pos];
        self.skip_whitespace();
        if self.bump() != Some('=') {
            return Step::Malformed;
        }
        self.skip_whitespace();
        let value = if self.peek() == Some('\'') {
            self.bump();
            self.quoted()
        } else {
            self.bare()
        };
        match value {
            Some(value) => Step::Param {
                text: &self.s[start..self.pos],
                key,
                value,
            },
            None => Step::Malformed,
        }
    }

    /// The rest of a quoted value, closing quote included.
    fn quoted(&mut self) -> Option<String> {
        let mut value = String::new();
        loop {
            match self.bump()? {
                '\'' => return Some(value),
                '\\' => value.push(self.bump()?),
                c => value.push(c),
            }
        }
    }

    fn bare(&mut self) -> Option<String> {
        let mut value = String::new();
        while let Some(c) = self.peek().filter(|c| !c.is_whitespace()) {
            self.bump();
            match c {
                '\\' => {
                    if let Some(escaped) = self.bump() {
                        value.push(escaped);
                    }
                }
                c => value.push(c),
            }
        }
        (!value.is_empty()).then_some(value)
    }
}

// --- negotiation and handshake ---------------------------------------------------------

/// A connected socket after TLS negotiation.
pub enum Stream<Io> {
    Plain(Io),
    Tls(Box<TlsStream<Io>>),
}

pub async fn write_all<Io: AsyncIo>(io: &mut Io, mut buf: &[u8]) -> io::Result<()> {
    while !buf.is_empty() {
        match io.write(buf) {
            Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
            Ok(n) => buf = &buf[n..],
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                io.ready(Interest::WRITABLE).await?;
            }
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

/// One byte: the server's answer to `SSLRequest` (`None` if it closed the connection).
async fn read_byte<Io: AsyncIo>(io: &mut Io) -> io::Result<Option<u8>> {
    let mut byte = [0u8];
    loop {
        match io.read(&mut byte) {
            Ok(0) => return Ok(None),
            Ok(_) => return Ok(Some(byte[0])),
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                io.ready(Interest::READABLE).await?;
            }
            Err(e) => return Err(e),
        }
    }
}

/// Ask the server for TLS (`SSLRequest`); `true` if it agreed.
async fn ssl_request<Io: AsyncIo>(io: &mut Io) -> Result<bool, QueryError> {
    let failed = |e: io::Error| interface(format!("SSL negotiation failed: {e}"));
    let mut buf = BytesMut::new();
    frontend::ssl_request(&mut buf);
    write_all(io, &buf).await.map_err(failed)?;
    // Exactly one byte: anything after an 'S' must be the TLS handshake (CVE-2021-23222).
    match read_byte(io).await.map_err(failed)? {
        Some(b'S') => Ok(true),
        Some(b'N') => Ok(false),
        Some(b'E') => Err(interface(
            "server sent an error response during SSL negotiation",
        )),
        Some(other) => Err(interface(format!(
            "received invalid response to SSL negotiation: {:?}",
            char::from(other)
        ))),
        None => Err(interface(
            "server closed the connection during SSL negotiation",
        )),
    }
}

/// A rustls error as a message that says what to do about it.
fn handshake_error(host: &str, e: &rustls::Error) -> QueryError {
    let hint = match e {
        rustls::Error::InvalidCertificate(CertificateError::UnknownIssuer) => {
            " (the server certificate is not signed by a trusted CA: set sslrootcert to the \
             CA certificate, or use sslmode=require to skip certificate verification)"
        }
        rustls::Error::InvalidCertificate(
            CertificateError::NotValidForName | CertificateError::NotValidForNameContext { .. },
        ) => {
            " (connect with a host name the certificate was issued for, or use \
             sslmode=verify-ca to skip the host name check)"
        }
        _ => "",
    };
    interface(format!("TLS handshake with {host} failed: {e}{hint}"))
}

/// Run the TLS handshake over `io` until application data can flow.
async fn handshake<Io: AsyncIo>(
    io: &mut Io,
    tls: &mut ClientConnection,
    host: &str,
) -> Result<(), QueryError> {
    let failed = |e: io::Error| interface(format!("TLS handshake with {host} failed: {e}"));
    loop {
        while tls.wants_write() {
            match tls.write_tls(io) {
                Ok(_) => {}
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    io.ready(Interest::WRITABLE).await.map_err(failed)?;
                }
                Err(e) => return Err(failed(e)),
            }
        }
        if !tls.is_handshaking() {
            return Ok(());
        }
        match tls.read_tls(io) {
            Ok(0) => {
                return Err(interface(format!(
                    "TLS handshake with {host} failed: the server closed the connection"
                )));
            }
            Ok(_) => {
                if let Err(e) = tls.process_new_packets() {
                    // Tell the server why (best effort: the alert is queued by rustls).
                    let _ = tls.write_tls(io);
                    return Err(handshake_error(host, &e));
                }
            }
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                io.ready(Interest::READABLE).await.map_err(failed)?;
            }
            Err(e) => return Err(failed(e)),
        }
    }
}

/// Negotiate TLS on a freshly connected TCP socket to `host`, as `opts` asks: the socket
/// as is (`disable`, or `prefer` against a server without TLS) or a TLS stream. Over TLS,
/// `cfg` gets the server certificate's `tls-server-end-point` hash, which xitca uses for
/// SCRAM-SHA-256-PLUS (channel binding).
pub async fn start<Io: AsyncIo>(
    mut io: Io,
    opts: &TlsOptions,
    host: &str,
    cfg: &mut Config,
) -> Result<Stream<Io>, QueryError> {
    if opts.mode == SslMode::Disable {
        return Ok(Stream::Plain(io));
    }
    // Configured first, so a bad setting (a missing file, ...) fails whatever the server says.
    let config = opts.client_config()?;
    if !opts.direct && !ssl_request(&mut io).await? {
        if opts.mode == SslMode::Prefer {
            return Ok(Stream::Plain(io));
        }
        return Err(interface(
            "server does not support SSL, but SSL was required",
        ));
    }
    let name = ServerName::try_from(host)
        .map_err(|e| interface(format!("invalid host name for TLS {host:?}: {e}")))?
        .to_owned();
    let mut tls = ClientConnection::new(config, name)
        .map_err(|e| interface(format!("TLS setup failed: {e}")))?;
    handshake(&mut io, &mut tls, host).await?;
    if opts.direct && tls.alpn_protocol() != Some(ALPN) {
        return Err(interface(
            "direct TLS connection was established without the \"postgresql\" ALPN protocol",
        ));
    }
    if let Some(hash) = tls
        .peer_certificates()
        .and_then(|certs| certs.first())
        .and_then(|cert| server_end_point(cert.as_ref()))
    {
        cfg.tls_server_end_point(hash);
    }
    Ok(Stream::Tls(Box::new(TlsStream {
        io,
        tls,
        read_ready: true,
        closing: false,
    })))
}

// --- channel binding -------------------------------------------------------------------
//
// Over TLS, PostgreSQL offers SCRAM-SHA-256-PLUS, and xitca 0.4, given no
// `tls-server-end-point` data, answers with the gs2 flag `y` ("I support binding but think
// you don't"), which the server rejects as a downgrade. So SCRAM over TLS through xitca
// (`channel_binding=prefer`) only works when the hash below is known; `disable` and
// `require` authenticate in `auth.rs` instead.

/// `1.2.840.113549.1.1`: PKCS #1 signature algorithms (the last arc picks the hash).
const PKCS1: &[u8] = &[0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01];
/// Last arc of `1.2.840.113549.1.1.10`: RSASSA-PSS, whose parameters name the hash.
const PKCS1_PSS: u8 = 10;
/// `1.2.840.10045.4.1`: ecdsa-with-SHA1.
const ECDSA_SHA1: &[u8] = &[0x2a, 0x86, 0x48, 0xce, 0x3d, 0x04, 0x01];
/// `1.2.840.10045.4.3`: ecdsa-with-SHA2 (the last arc picks the hash).
const ECDSA_SHA2: &[u8] = &[0x2a, 0x86, 0x48, 0xce, 0x3d, 0x04, 0x03];
/// `2.16.840.1.101.3.4.2`: the SHA-2 hash functions (the last arc picks one).
const SHA2: &[u8] = &[0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x02];
/// `1.3.14.3.2.26`: SHA-1.
const SHA1: &[u8] = &[0x2b, 0x0e, 0x03, 0x02, 0x1a];

const SEQUENCE: u8 = 0x30;
const OID: u8 = 0x06;
/// `[0] EXPLICIT`: the hash algorithm in RSASSA-PSS parameters.
const CONTEXT_0: u8 = 0xa0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Hash {
    Sha224,
    Sha256,
    Sha384,
    Sha512,
}

impl Hash {
    fn digest(self, data: &[u8]) -> Vec<u8> {
        match self {
            Self::Sha224 => Sha224::digest(data).to_vec(),
            Self::Sha256 => Sha256::digest(data).to_vec(),
            Self::Sha384 => Sha384::digest(data).to_vec(),
            Self::Sha512 => Sha512::digest(data).to_vec(),
        }
    }

    /// A hash function's OID; SHA-1 is upgraded to SHA-256 (RFC 5929).
    fn from_oid(oid: &[u8]) -> Option<Self> {
        if oid == SHA1 {
            return Some(Self::Sha256);
        }
        match oid.split_last()? {
            (1, SHA2) => Some(Self::Sha256),
            (2, SHA2) => Some(Self::Sha384),
            (3, SHA2) => Some(Self::Sha512),
            (4, SHA2) => Some(Self::Sha224),
            _ => None,
        }
    }

    /// The hash of an RSASSA-PSS signature: its parameters' `hashAlgorithm`, SHA-1 (so
    /// SHA-256 here) when left at the default.
    fn from_pss_params(params: &[u8]) -> Option<Self> {
        let (params, _) = der(params, SEQUENCE)?;
        let Some((hash_algorithm, _)) = der(params, CONTEXT_0) else {
            return Some(Self::Sha256);
        };
        let (hash_algorithm, _) = der(hash_algorithm, SEQUENCE)?;
        Self::from_oid(der(hash_algorithm, OID)?.0)
    }

    /// The hash a certificate signature algorithm uses (MD5 and SHA-1 upgraded to SHA-256).
    fn of_signature(oid: &[u8], params: &[u8]) -> Option<Self> {
        if oid == ECDSA_SHA1 {
            return Some(Self::Sha256);
        }
        match oid.split_last()? {
            (4 | 5 | 11, PKCS1) => Some(Self::Sha256),
            (12, PKCS1) => Some(Self::Sha384),
            (13, PKCS1) => Some(Self::Sha512),
            (14, PKCS1) => Some(Self::Sha224),
            (&PKCS1_PSS, PKCS1) => Self::from_pss_params(params),
            (1, ECDSA_SHA2) => Some(Self::Sha224),
            (2, ECDSA_SHA2) => Some(Self::Sha256),
            (3, ECDSA_SHA2) => Some(Self::Sha384),
            (4, ECDSA_SHA2) => Some(Self::Sha512),
            _ => None,
        }
    }
}

/// Split one DER element tagged `tag` off `input`: `(contents, rest)`.
fn der(input: &[u8], tag: u8) -> Option<(&[u8], &[u8])> {
    let (&found, input) = input.split_first()?;
    let (&first, mut input) = input.split_first()?;
    if found != tag {
        return None;
    }
    let len = if first < 0x80 {
        usize::from(first)
    } else {
        let width = usize::from(first & 0x7f);
        if width == 0 || width > 4 || input.len() < width {
            return None;
        }
        let (bytes, rest) = input.split_at(width);
        input = rest;
        bytes.iter().fold(0usize, |n, b| (n << 8) | usize::from(*b))
    };
    (input.len() >= len).then(|| input.split_at(len))
}

/// RFC 5929 `tls-server-end-point` data for `cert`: the certificate hashed with its
/// signature's hash (SHA-256 for MD5 and SHA-1), as PostgreSQL computes it. `None` when
/// there is no such hash (Ed25519, Ed448), which PostgreSQL can't bind to either.
fn server_end_point(cert: &[u8]) -> Option<Vec<u8>> {
    let (certificate, _) = der(cert, SEQUENCE)?;
    let (_tbs, rest) = der(certificate, SEQUENCE)?;
    let (algorithm, _) = der(rest, SEQUENCE)?;
    let (oid, params) = der(algorithm, OID)?;
    Some(Hash::of_signature(oid, params)?.digest(cert))
}

// --- the TLS stream --------------------------------------------------------------------

/// An `AsyncIo` that runs a rustls session over another `AsyncIo`.
///
/// Readiness comes from the inner socket, except that after a read that did not end in
/// `WouldBlock` (plaintext may still be buffered in rustls, or the session has ended) the
/// stream reports itself readable without waiting. Writes only take new plaintext once
/// the previous records are on the wire, so at most one batch of ciphertext (rustls' 64 KiB
/// limit) is buffered and `WouldBlock` still means "the socket is full".
pub struct TlsStream<Io> {
    io: Io,
    tls: ClientConnection,
    read_ready: bool,
    /// `close_notify` was queued by `poll_shutdown`.
    closing: bool,
}

impl<Io: AsyncIo> TlsStream<Io> {
    /// Write queued TLS records until none is left (`WouldBlock` if the socket is full).
    fn flush_tls(&mut self) -> io::Result<()> {
        while self.tls.wants_write() {
            if self.tls.write_tls(&mut self.io)? == 0 {
                return Err(io::ErrorKind::WriteZero.into());
            }
        }
        Ok(())
    }

    fn read_plaintext(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        loop {
            match self.tls.reader().read(buf) {
                // Ok(0): the server sent close_notify. UnexpectedEof: the socket closed
                // without one.
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                result => return result,
            }
            // Nothing decrypted is buffered: read more records. (Never with plaintext left
            // over, so rustls' plaintext buffer can't fill up.) EOF is recorded by rustls
            // and reported by the next `reader().read`.
            self.tls.read_tls(&mut self.io)?;
            if let Err(e) = self.tls.process_new_packets() {
                let _ = self.flush_tls(); // best effort: the alert rustls queued
                return Err(io::Error::new(io::ErrorKind::InvalidData, e));
            }
            if self.tls.wants_write() {
                // A record to answer (e.g. a key update); sent with the next write if the
                // socket is full now.
                match self.flush_tls() {
                    Err(e) if e.kind() != io::ErrorKind::WouldBlock => return Err(e),
                    _ => {}
                }
            }
        }
    }
}

impl<Io: AsyncIo> Read for TlsStream<Io> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let result = self.read_plaintext(buf);
        self.read_ready = !matches!(&result, Err(e) if e.kind() == io::ErrorKind::WouldBlock);
        result
    }
}

impl<Io: AsyncIo> Write for TlsStream<Io> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        if buf.is_empty() {
            return Ok(0);
        }
        self.flush_tls()?;
        // With nothing queued, rustls takes up to its buffer limit (64 KiB): never 0 bytes.
        let n = self.tls.writer().write(buf)?;
        match self.flush_tls() {
            Err(e) if e.kind() != io::ErrorKind::WouldBlock => Err(e),
            // The rest goes out with the next write or flush.
            _ => Ok(n),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        self.flush_tls()?;
        self.io.flush()
    }
}

impl<Io: AsyncIo + Send> AsyncIo for TlsStream<Io> {
    async fn ready(&mut self, interest: Interest) -> io::Result<Ready> {
        if interest.is_readable() && self.read_ready {
            return Ok(Ready::READABLE);
        }
        self.io.ready(interest).await
    }

    fn poll_ready(&mut self, interest: Interest, cx: &mut Context<'_>) -> Poll<io::Result<Ready>> {
        if interest.is_readable() && self.read_ready {
            return Poll::Ready(Ok(Ready::READABLE));
        }
        // One direction at a time: tokio's streams can't poll both at once.
        let mut ready = Ready::EMPTY;
        if interest.is_readable()
            && let Poll::Ready(result) = self.io.poll_ready(Interest::READABLE, cx)
        {
            ready |= result?;
        }
        if interest.is_writable()
            && let Poll::Ready(result) = self.io.poll_ready(Interest::WRITABLE, cx)
        {
            ready |= result?;
        }
        if ready.is_empty() {
            Poll::Pending
        } else {
            Poll::Ready(Ok(ready))
        }
    }

    fn is_vectored_write(&self) -> bool {
        false
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if !this.closing {
            this.closing = true;
            this.tls.send_close_notify();
        }
        loop {
            match this.flush_tls() {
                Ok(()) => break,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    match this.io.poll_ready(Interest::WRITABLE, cx) {
                        Poll::Ready(Ok(_)) => {}
                        Poll::Ready(Err(e)) => return Poll::Ready(Err(e)),
                        Poll::Pending => return Poll::Pending,
                    }
                }
                Err(e) => return Poll::Ready(Err(e)),
            }
        }
        Pin::new(&mut this.io).poll_shutdown(cx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn split_all(dsn: &str) -> (String, DsnOptions) {
        let (rest, opts) = split_dsn(dsn).unwrap_or_else(|e| match e {
            QueryError::Interface(m) => panic!("{m}"),
            _ => panic!("unexpected error"),
        });
        (rest.into_owned(), opts)
    }

    fn split(dsn: &str) -> (String, TlsOptions) {
        let (rest, opts) = split_all(dsn);
        (rest, opts.tls)
    }

    #[test]
    fn connect_timeout_is_split_off_as_libpq_reads_it() {
        let (rest, opts) = split_all("postgresql://db/x?connect_timeout=10&application_name=a");
        assert_eq!(rest, "postgresql://db/x?application_name=a");
        assert_eq!(opts.connect_timeout, Some(10));
        let (rest, opts) = split_all("host=db connect_timeout=' 5 ' user=u");
        assert_eq!(rest, "host=db user=u");
        assert_eq!(opts.connect_timeout, Some(5));
        for (value, seconds) in [("1", Some(2)), ("0", None), ("-3", None), ("3", Some(3))] {
            let dsn = format!("postgresql://db/x?connect_timeout={value}");
            assert_eq!(split_all(&dsn).1.connect_timeout, seconds, "{value}");
        }
        assert_eq!(split_all("postgresql://db/x").1.connect_timeout, None);
        assert_eq!(
            error("postgresql://db/x?connect_timeout=1.5"),
            "invalid integer value \"1.5\" for connection option \"connect_timeout\""
        );
    }

    fn error(dsn: &str) -> String {
        match split_dsn(dsn) {
            Err(QueryError::Interface(m)) => m,
            _ => panic!("expected an InterfaceError for {dsn:?}"),
        }
    }

    #[test]
    fn url_dsn_tls_keys_are_split_off() {
        let (rest, opts) = split(
            "postgresql://u:p%3F@db:5432/x?application_name=a&sslmode=verify-full\
             &sslrootcert=%2Ftmp%2Fca.pem&options=-c%20x%3D1",
        );
        assert_eq!(
            rest,
            "postgresql://u:p%3F@db:5432/x?application_name=a&options=-c%20x%3D1"
        );
        assert_eq!(opts.mode, SslMode::VerifyFull);
        assert_eq!(opts.root_cert, Some(RootCert::File("/tmp/ca.pem".into())));
        let (rest, opts) = split("postgres://db/x?sslmode=require");
        assert_eq!(rest, "postgres://db/x");
        assert_eq!(opts.mode, SslMode::Require);
        let (rest, opts) = split("postgresql://u:p@db/x");
        assert_eq!(rest, "postgresql://u:p@db/x");
        assert_eq!(opts.mode, SslMode::Prefer, "libpq's default");
        assert!(Config::try_from(rest.as_str()).is_ok());
    }

    #[test]
    fn unknown_keys_are_named() {
        let unknown = "unsupported connection option \"keepalives\"";
        assert_eq!(
            error("postgresql://db/x?keepalives=1&sslmode=require"),
            unknown
        );
        assert_eq!(error("host=db keepalives=1 user=u"), unknown);
        let (rest, _) = split(
            "postgresql://u@db/x?application_name=a&options=-c%20x%3D1&target_session_attrs=any",
        );
        assert!(
            Config::try_from(rest.as_str()).is_ok(),
            "xitca's own keys pass"
        );
    }

    #[test]
    fn key_value_dsn_tls_keys_are_split_off() {
        let (rest, opts) = split(
            "host=db  sslmode = 'verify-ca' user=u password='it\\'s x' sslrootcert=/c\\ a.pem",
        );
        assert_eq!(rest, "host=db user=u password='it\\'s x'");
        assert_eq!(opts.mode, SslMode::VerifyCa);
        assert_eq!(opts.root_cert, Some(RootCert::File("/c a.pem".into())));
        let cfg = Config::try_from(rest.as_str()).unwrap_or_else(|_| panic!("xitca rejects"));
        assert_eq!(cfg.get_password(), Some(&b"it's x"[..]));
        assert_eq!(split("host=db user=u").0, "host=db user=u");
    }

    #[test]
    fn libpq_option_rules() {
        assert_eq!(
            split("host=db sslrootcert=system").1.mode,
            SslMode::VerifyFull
        );
        assert!(error("host=db sslrootcert=system sslmode=require").contains("weak sslmode"));
        assert!(error("host=db sslnegotiation=direct").contains("weak sslmode \"prefer\""));
        assert!(
            split("host=db sslnegotiation=direct sslmode=require")
                .1
                .direct
        );
        assert!(error("host=db sslmode=allow").contains("not supported"));
        assert!(error("host=db sslmode=bogus").contains("invalid sslmode"));
        assert!(error("host=db sslcert=/c.pem").contains("together"));
        assert!(error("host=db sslpassword=x").contains("not supported"));
        assert!(!split("host=db sslsni=0").1.sni);
    }

    #[test]
    fn channel_binding_values() {
        let binding = |dsn: &str| split(dsn).1.channel_binding();
        assert_eq!(
            binding("host=db"),
            ChannelBinding::Prefer,
            "libpq's default"
        );
        assert_eq!(
            binding("host=db channel_binding=prefer"),
            ChannelBinding::Prefer
        );
        assert_eq!(
            binding("host=db channel_binding=disable"),
            ChannelBinding::Disable
        );
        assert_eq!(
            binding("host=db channel_binding=require"),
            ChannelBinding::Require
        );
        let (rest, opts) = split("postgresql://u:p@db/x?sslmode=require&channel_binding=require");
        assert_eq!(rest, "postgresql://u:p@db/x", "not passed to xitca");
        assert_eq!(opts.channel_binding(), ChannelBinding::Require);
        assert_eq!(
            binding("host=db sslmode=disable channel_binding=disable"),
            ChannelBinding::Disable
        );
        let message = error("host=db channel_binding=bogus");
        assert!(
            message.contains("invalid channel_binding value: \"bogus\"")
                && message.contains("\"disable\", \"prefer\" or \"require\""),
            "{message}"
        );
        assert!(
            error("host=db sslmode=disable channel_binding=require")
                .contains("channel_binding=require needs TLS")
        );
    }

    /// One DER element (short-form length).
    fn tlv(tag: u8, contents: &[u8]) -> Vec<u8> {
        [&[tag, contents.len() as u8], contents].concat()
    }

    fn oid(family: &[u8], last: u8) -> Vec<u8> {
        [family, &[last]].concat()
    }

    /// A DER "certificate" with only the parts `server_end_point` looks at.
    fn fake_cert(oid: &[u8], params: &[u8]) -> Vec<u8> {
        let algorithm = tlv(SEQUENCE, &[tlv(OID, oid), params.to_vec()].concat());
        let signature = tlv(0x03, &[0]);
        tlv(
            SEQUENCE,
            &[tlv(SEQUENCE, &[]), algorithm, signature].concat(),
        )
    }

    #[test]
    fn end_point_hash_follows_the_signature_algorithm() {
        let of = |oid: &[u8]| Hash::of_signature(oid, &[]);
        assert_eq!(of(&oid(PKCS1, 4)), Some(Hash::Sha256), "md5WithRSA");
        assert_eq!(of(&oid(PKCS1, 5)), Some(Hash::Sha256), "sha1WithRSA");
        assert_eq!(of(&oid(PKCS1, 11)), Some(Hash::Sha256));
        assert_eq!(of(&oid(PKCS1, 12)), Some(Hash::Sha384));
        assert_eq!(of(&oid(PKCS1, 13)), Some(Hash::Sha512));
        assert_eq!(of(&oid(PKCS1, 14)), Some(Hash::Sha224));
        assert_eq!(of(ECDSA_SHA1), Some(Hash::Sha256));
        assert_eq!(of(&oid(ECDSA_SHA2, 2)), Some(Hash::Sha256));
        assert_eq!(of(&oid(ECDSA_SHA2, 3)), Some(Hash::Sha384));
        assert_eq!(of(&oid(ECDSA_SHA2, 4)), Some(Hash::Sha512));
        assert_eq!(of(&[0x2b, 0x65, 0x70]), None, "Ed25519");

        // RSASSA-PSS: the hash comes from the parameters (SHA-1, so SHA-256, by default).
        let pss = oid(PKCS1, PKCS1_PSS);
        let pss_with = |hash: &[u8]| {
            let hash_algorithm = tlv(SEQUENCE, &tlv(OID, hash));
            tlv(SEQUENCE, &tlv(CONTEXT_0, &hash_algorithm))
        };
        let sha384 = oid(SHA2, 2);
        assert_eq!(
            Hash::of_signature(&pss, &pss_with(&sha384)),
            Some(Hash::Sha384)
        );
        assert_eq!(
            Hash::of_signature(&pss, &pss_with(SHA1)),
            Some(Hash::Sha256)
        );
        assert_eq!(
            Hash::of_signature(&pss, &tlv(SEQUENCE, &[])),
            Some(Hash::Sha256)
        );
        assert_eq!(Hash::of_signature(&pss, &[]), None, "missing parameters");

        let cert = fake_cert(&oid(ECDSA_SHA2, 3), &[]);
        assert_eq!(
            server_end_point(&cert),
            Some(Sha384::digest(&cert).to_vec())
        );
        let cert = fake_cert(&pss, &pss_with(&oid(SHA2, 3)));
        assert_eq!(
            server_end_point(&cert),
            Some(Sha512::digest(&cert).to_vec())
        );
        assert_eq!(server_end_point(b"\x30\x05garbage"), None);
    }
}
