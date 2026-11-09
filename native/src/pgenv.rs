//! libpq's environment variables and password file, for the settings a DSN leaves out.
//!
//! * "Environment Variables" in the libpq docs
//!   (<https://www.postgresql.org/docs/current/libpq-envars.html>): a `PG*` variable gives
//!   the value of its key when the DSN doesn't. `tls.rs` and `net.rs` apply them to the keys
//!   they parse.
//! * "The Password File" (<https://www.postgresql.org/docs/current/libpq-pgpass.html>) and
//!   libpq's `passwordFromFile` (`fe-connect.c`): the password, when neither the DSN nor
//!   `PGPASSWORD` gives one.
//! * With no host and no user from either, libpq's defaults: the local unix socket
//!   ([`socket_dir`]) and the OS user's login name ([`os_user`]).

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

/// Looks up an environment variable. The process environment ([`process_env`]), or a fixed
/// one in tests.
pub type Env<'a> = &'a dyn Fn(&str) -> Option<String>;

/// The process environment. An empty (or non-UTF-8) variable counts as unset.
pub fn process_env(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.is_empty())
}

/// `~/.pgpass`: `HOME`, else the user's home directory from the password database, as in
/// libpq.
pub fn default_passfile() -> Option<PathBuf> {
    std::env::home_dir().map(|home| home.join(".pgpass"))
}

/// What the password file gave.
#[derive(Debug, PartialEq, Eq)]
pub enum Passfile {
    /// The password of the first line that matches.
    Password(Vec<u8>),
    /// No line matches, or there is no file (or it can't be read).
    Nothing,
    /// The file is ignored, for this reason (libpq warns about it on stderr).
    Ignored(String),
}

/// Look up the password for `host`, `port`, `dbname` and `user` in the password file at
/// `path`. As in libpq: a file that is not a plain file, or that the group or others have any
/// access to, is ignored with a warning; one that is missing or unreadable is ignored quietly.
pub fn lookup(path: &Path, host: &str, port: u16, dbname: &str, user: &str) -> Passfile {
    let Ok(meta) = fs::metadata(path) else {
        return Passfile::Nothing;
    };
    if !meta.is_file() {
        return Passfile::Ignored(format!(
            "password file \"{}\" is not a plain file",
            path.display()
        ));
    }
    if meta.permissions().mode() & 0o077 != 0 {
        return Passfile::Ignored(format!(
            "password file \"{}\" has group or world access; permissions should be u=rw \
             (0600) or less",
            path.display()
        ));
    }
    let Ok(text) = fs::read(path) else {
        return Passfile::Nothing;
    };
    let host = if DEFAULT_SOCKET_DIRS.contains(&host) {
        "localhost"
    } else {
        host
    };
    let port = port.to_string();
    find(&text, [host, &port, dbname, user]).map_or(Passfile::Nothing, Passfile::Password)
}

/// libpq's default socket directory, which is set when libpq is built: `/tmp` upstream,
/// `/var/run/postgresql` in the Debian, Ubuntu and Red Hat packages (`/run/postgresql` is the
/// same directory). fastpg has none of its own, so it uses all of them: with no host it
/// connects to the first that holds the server's socket ([`socket_dir`]), and the password
/// file is looked up as `localhost` for any of them. Any other socket directory is matched
/// as written.
pub const DEFAULT_SOCKET_DIRS: &[&str] = &["/var/run/postgresql", "/run/postgresql", "/tmp"];

/// The first of `dirs` holding the socket of a server on `port` (`.s.PGSQL.<port>`), for a
/// connection with no host; else the error, naming each path tried.
pub fn socket_dir<'a>(dirs: &[&'a str], port: u16) -> Result<&'a str, String> {
    let socket = |dir: &str| Path::new(dir).join(format!(".s.PGSQL.{port}"));
    if let Some(dir) = dirs.iter().find(|dir| socket(dir).exists()) {
        return Ok(dir);
    }
    let tried: Vec<_> = dirs
        .iter()
        .map(|dir| socket(dir).display().to_string())
        .collect();
    Err(format!(
        "no host given, and no local server socket found (tried {}); set host= or PGHOST",
        tried.join(", ")
    ))
}

/// The login name of the effective user, from the password database: libpq's default user
/// (it doesn't read `USER`).
pub fn os_user() -> Result<String, String> {
    // SAFETY: `geteuid` has no preconditions and cannot fail.
    let uid = unsafe { libc::geteuid() };
    let mut buf = vec![0 as libc::c_char; 1024];
    loop {
        // SAFETY: an all-zero `passwd` is valid (null pointers and zero ids).
        let mut pwd: libc::passwd = unsafe { std::mem::zeroed() };
        let mut found = std::ptr::null_mut();
        // SAFETY: every pointer is valid for the call, and `buf.len()` is `buf`'s size.
        let rc = unsafe {
            libc::getpwuid_r(
                uid,
                &raw mut pwd,
                buf.as_mut_ptr(),
                buf.len(),
                &raw mut found,
            )
        };
        if rc == libc::ERANGE && buf.len() < 1 << 20 {
            buf.resize(buf.len() * 2, 0);
            continue;
        }
        if rc != 0 {
            let reason = std::io::Error::from_raw_os_error(rc);
            return Err(format!("could not look up local user ID {uid}: {reason}"));
        }
        if found.is_null() {
            return Err(format!(
                "could not look up local user ID {uid}: user does not exist"
            ));
        }
        // SAFETY: on success `pw_name` points to a NUL-terminated string in `buf`.
        let name = unsafe { std::ffi::CStr::from_ptr(pwd.pw_name) };
        return Ok(name.to_string_lossy().into_owned());
    }
}

/// The password of the first line of `text` whose first four fields match `keys` (host,
/// port, database, user). Lines starting with `#` are comments; a trailing `\r` is dropped.
fn find(text: &[u8], keys: [&str; 4]) -> Option<Vec<u8>> {
    text.split(|&b| b == b'\n').find_map(|line| {
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        if line.is_empty() || line.starts_with(b"#") {
            return None;
        }
        let password = keys
            .iter()
            .try_fold(line, |rest, key| field(rest, key.as_bytes()))?;
        Some(unescape(password))
    })
}

/// libpq's `pwdfMatchesString`: if the first field of `line` is `*` or `token`, the rest of
/// the line after it. In a field, `\` makes the next byte literal (so `\:` is a `:` and `\\`
/// a `\`).
fn field<'a>(line: &'a [u8], token: &[u8]) -> Option<&'a [u8]> {
    if let Some(rest) = line.strip_prefix(b"*:") {
        return Some(rest);
    }
    let mut token = token;
    let mut i = 0;
    loop {
        let mut byte = *line.get(i)?;
        let escaped = byte == b'\\';
        if escaped {
            i += 1;
            byte = *line.get(i)?;
        }
        if byte == b':' && !escaped && token.is_empty() {
            return Some(&line[i + 1..]);
        }
        let (&first, rest) = token.split_first()?;
        if byte != first {
            return None;
        }
        token = rest;
        i += 1;
    }
}

/// The password field: up to the next unescaped `:`, each `\` dropped before the byte it
/// escapes (a trailing one is kept), as libpq reads it.
fn unescape(field: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(field.len());
    let mut bytes = field.iter().peekable();
    while let Some(&byte) = bytes.next() {
        match byte {
            b':' => break,
            b'\\' => out.push(bytes.next().copied().unwrap_or(b'\\')),
            _ => out.push(byte),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEYS: [&str; 4] = ["db.example", "5432", "app", "alice"];

    fn found(text: &str, keys: [&str; 4]) -> Option<String> {
        find(text.as_bytes(), keys).map(|p| String::from_utf8(p).unwrap_or_default())
    }

    #[test]
    fn a_line_matches_on_all_four_fields() {
        let text = "db.example:5432:app:alice:secret\n";
        assert_eq!(found(text, KEYS).as_deref(), Some("secret"));
        for keys in [
            ["other", "5432", "app", "alice"],
            ["db.example", "5433", "app", "alice"],
            ["db.example", "5432", "other", "alice"],
            ["db.example", "5432", "app", "bob"],
            // A field matches the whole value, not a prefix of it.
            ["db.ex", "5432", "app", "alice"],
            ["db.example.org", "5432", "app", "alice"],
        ] {
            assert_eq!(found(text, keys), None, "{keys:?}");
        }
    }

    #[test]
    fn a_star_matches_anything() {
        assert_eq!(found("*:*:*:*:pw", KEYS).as_deref(), Some("pw"));
        assert_eq!(found("db.example:*:app:*:pw", KEYS).as_deref(), Some("pw"));
        // Only a whole-field `*`: `*x` is the literal text.
        assert_eq!(found("*x:*:*:*:pw", KEYS), None);
        assert_eq!(
            found("*x:*:*:*:pw", ["*x", "1", "d", "u"]).as_deref(),
            Some("pw")
        );
    }

    #[test]
    fn the_first_matching_line_wins_and_comments_are_skipped() {
        let text = "# db.example:5432:app:alice:commented\n\
                    other:*:*:*:other\n\
                    \n\
                    *:5432:*:alice:first\r\n\
                    *:*:*:*:second\n";
        assert_eq!(found(text, KEYS).as_deref(), Some("first"));
        assert_eq!(found(text, ["h", "1", "d", "u"]).as_deref(), Some("second"));
        // The last line needs no newline.
        assert_eq!(found("x:*:*:*:a\n*:*:*:*:b", KEYS).as_deref(), Some("b"));
    }

    #[test]
    fn backslash_escapes_colon_and_backslash() {
        let keys = ["h", "1", "d", "we:ird\\user"];
        let text = r"h:1:d:we\:ird\\user:pa\:ss\\word";
        assert_eq!(found(text, keys).as_deref(), Some(r"pa:ss\word"));
        // Unescaped, a `:` ends the field once the value is matched; before that it matches a
        // `:` in the value (libpq's quirk).
        assert_eq!(found(r"h:1:d:we:ird\\user:pw", keys).as_deref(), Some("pw"));
        assert_eq!(found(r"h:1:d:we:pw", keys), None);
        // The password ends at an unescaped `:`; a trailing `\` is kept.
        assert_eq!(found("*:*:*:*:pw:extra", KEYS).as_deref(), Some("pw"));
        assert_eq!(found(r"*:*:*:*:pw\", KEYS).as_deref(), Some(r"pw\"));
        // Any byte may be escaped.
        assert_eq!(found(r"db\.example:*:*:*:pw", KEYS).as_deref(), Some("pw"));
    }

    #[test]
    fn short_lines_never_match() {
        for text in [
            "db.example:5432:app:alice",
            "db.example:5432:app",
            "*:*:*",
            r"*:*:*:alice\",
        ] {
            assert_eq!(found(text, KEYS), None, "{text}");
        }
        // An empty password is a match.
        assert_eq!(found("*:*:*:*:", KEYS).as_deref(), Some(""));
    }

    fn passfile(name: &str, text: &str, mode: u32) -> PathBuf {
        let path =
            std::env::temp_dir().join(format!("fastpg-pgpass-{}-{name}", std::process::id()));
        fs::write(&path, text).unwrap_or_else(|e| panic!("{e}"));
        fs::set_permissions(&path, fs::Permissions::from_mode(mode))
            .unwrap_or_else(|e| panic!("{e}"));
        path
    }

    #[test]
    fn a_file_others_can_access_is_ignored() {
        let text = "*:*:*:*:pw\n";
        let private = passfile("private", text, 0o600);
        assert_eq!(
            lookup(&private, "h", 1, "d", "u"),
            Passfile::Password(b"pw".to_vec())
        );
        for (name, mode) in [("group", 0o640), ("world", 0o604), ("exec", 0o610)] {
            let path = passfile(name, text, mode);
            let Passfile::Ignored(message) = lookup(&path, "h", 1, "d", "u") else {
                panic!("{mode:o}: not ignored");
            };
            assert!(message.contains("has group or world access"), "{message}");
            let _ = fs::remove_file(path);
        }
        let _ = fs::remove_file(private);
        let missing = std::env::temp_dir().join("fastpg-pgpass-missing");
        assert_eq!(lookup(&missing, "h", 1, "d", "u"), Passfile::Nothing);
        let Passfile::Ignored(message) = lookup(&std::env::temp_dir(), "h", 1, "d", "u") else {
            panic!("a directory: not ignored");
        };
        assert!(message.ends_with("is not a plain file"), "{message}");
    }

    #[test]
    fn a_default_socket_directory_is_looked_up_as_localhost() {
        let path = passfile(
            "socket",
            "localhost:*:*:*:local\n/srv/pg:*:*:*:srv\n",
            0o600,
        );
        let password = |host| lookup(&path, host, 5432, "d", "u");
        for dir in DEFAULT_SOCKET_DIRS {
            assert_eq!(
                password(dir),
                Passfile::Password(b"local".to_vec()),
                "{dir}"
            );
        }
        assert_eq!(password("localhost"), Passfile::Password(b"local".to_vec()));
        assert_eq!(password("/srv/pg"), Passfile::Password(b"srv".to_vec()));
        assert_eq!(password("/elsewhere"), Passfile::Nothing);
        let _ = fs::remove_file(path);
    }

    #[test]
    fn the_first_directory_holding_the_socket_is_used() {
        let base = std::env::temp_dir().join(format!("fastpg-sockets-{}", std::process::id()));
        let text = |name: &str| base.join(name).to_string_lossy().into_owned();
        let (missing, empty, full, other) =
            (text("missing"), text("empty"), text("full"), text("other"));
        for dir in [&empty, &full, &other] {
            fs::create_dir_all(dir).unwrap_or_else(|e| panic!("{e}"));
        }
        for dir in [&full, &other] {
            fs::write(Path::new(dir).join(".s.PGSQL.6543"), "").unwrap_or_else(|e| panic!("{e}"));
        }
        let dirs = [
            missing.as_str(),
            empty.as_str(),
            full.as_str(),
            other.as_str(),
        ];
        assert_eq!(socket_dir(&dirs, 6543), Ok(full.as_str()));
        // The socket's name holds the port.
        assert_eq!(
            socket_dir(&dirs, 5432),
            Err(format!(
                "no host given, and no local server socket found (tried \
                 {missing}/.s.PGSQL.5432, {empty}/.s.PGSQL.5432, {full}/.s.PGSQL.5432, \
                 {other}/.s.PGSQL.5432); set host= or PGHOST"
            ))
        );
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn the_os_user_is_the_effective_users_login_name() {
        let out = std::process::Command::new("id")
            .arg("-un")
            .output()
            .unwrap_or_else(|e| panic!("{e}"));
        let name = String::from_utf8_lossy(&out.stdout).trim().to_owned();
        assert!(!name.is_empty());
        assert_eq!(os_user(), Ok(name));
    }
}
