//! Spotting `COPY ... FROM STDIN` and `COPY ... TO STDOUT` before they are sent.
//!
//! The query methods can't run them. The server would answer with COPY data or wait for
//! it, which xitca-postgres can only report as an unexpected message; and while a COPY FROM
//! STDIN waits, the server would swallow the next statement pipelined on the connection
//! (another caller's, on a pool) as a protocol error. So they are refused before anything
//! is sent. `copy_in()` is the supported way to load rows.
//!
//! This is a scan, not a parser: words, skipping comments, string literals, quoted
//! identifiers and dollar-quoted strings. COPY to or from the client can't run inside a
//! function or a `DO` block (the server refuses it there), so a statement starting with
//! `COPY` is the only place it can appear.

/// The error message for a refused statement.
pub const REFUSED: &str = "COPY ... FROM STDIN and COPY ... TO STDOUT are not supported by \
                           select_*(), execute() or execute_script(): use copy_in() to load rows";

/// Whether any of the `;`-separated statements in `sql` is a COPY from or to the client:
/// it starts with `COPY` and has `FROM STDIN` or `TO STDOUT` outside parentheses (so not
/// in a column list or a query), quotes and comments.
pub fn copies_with_client(sql: &str) -> bool {
    let bytes = sql.as_bytes();
    let mut i = 0;
    // Words seen in the current statement, and whether its first one was COPY.
    let mut words = 0usize;
    let mut copy = false;
    // Parenthesis depth, and the word just read outside parentheses (empty once anything
    // but whitespace or a comment follows it).
    let mut depth = 0usize;
    let mut last: &[u8] = b"";
    while i < bytes.len() {
        let mut word: &[u8] = b"";
        match bytes[i..] {
            [b' ' | b'\t' | b'\n' | b'\r' | b'\x0b' | b'\x0c', ..] => {
                i += 1;
                continue;
            }
            [b'-', b'-', ..] => {
                i += bytes[i..]
                    .iter()
                    .position(|&b| b == b'\n' || b == b'\r')
                    .unwrap_or(bytes.len() - i);
                continue;
            }
            [b'/', b'*', ..] => {
                i = after_block_comment(bytes, i + 2);
                continue;
            }
            [b';', ..] => {
                (words, copy, depth) = (0, false, 0);
                i += 1;
            }
            [b'(', ..] => {
                depth += 1;
                i += 1;
            }
            [b')', ..] => {
                depth = depth.saturating_sub(1);
                i += 1;
            }
            [b'\'', ..] => {
                // An E'...' string (the `E` was just read as a word) also escapes with `\`.
                let escapes = i > 0 && bytes[i - 1].eq_ignore_ascii_case(&b'e') && {
                    i < 2 || !is_word_byte(bytes[i - 2])
                };
                i = after_quoted(bytes, i + 1, b'\'', escapes);
            }
            [b'"', ..] => i = after_quoted(bytes, i + 1, b'"', false),
            [b'$', ..] => i = after_dollar(bytes, i),
            [b, ..] if is_word_start(b) => {
                let end = bytes[i..]
                    .iter()
                    .position(|&b| !is_word_byte(b))
                    .map_or(bytes.len(), |n| i + n);
                word = &bytes[i..end];
                if words == 0 {
                    copy = word.eq_ignore_ascii_case(b"copy");
                } else if copy && depth == 0 && to_or_from_client(last, word) {
                    return true;
                }
                words += 1;
                i = end;
            }
            _ => i += 1,
        }
        last = if depth == 0 { word } else { b"" };
    }
    false
}

/// `FROM STDIN` or `TO STDOUT`.
fn to_or_from_client(previous: &[u8], word: &[u8]) -> bool {
    (previous.eq_ignore_ascii_case(b"from") && word.eq_ignore_ascii_case(b"stdin"))
        || (previous.eq_ignore_ascii_case(b"to") && word.eq_ignore_ascii_case(b"stdout"))
}

fn is_word_start(b: u8) -> bool {
    b.is_ascii_alphabetic() || b == b'_' || b >= 0x80
}

fn is_word_byte(b: u8) -> bool {
    is_word_start(b) || b.is_ascii_digit() || b == b'$'
}

/// Where a `/* */` comment (which nests in PostgreSQL) ends, given where its body starts.
fn after_block_comment(bytes: &[u8], mut i: usize) -> usize {
    let mut depth = 1usize;
    while i < bytes.len() && depth > 0 {
        match bytes[i..] {
            [b'*', b'/', ..] => {
                depth -= 1;
                i += 2;
            }
            [b'/', b'*', ..] => {
                depth += 1;
                i += 2;
            }
            _ => i += 1,
        }
    }
    i
}

/// Where a string or identifier quoted with `quote` ends (a doubled quote is part of it),
/// given where its body starts.
fn after_quoted(bytes: &[u8], mut i: usize, quote: u8, escapes: bool) -> usize {
    while i < bytes.len() {
        if escapes && bytes[i] == b'\\' {
            i += 2;
        } else if bytes[i] == quote {
            if bytes.get(i + 1) == Some(&quote) {
                i += 2;
            } else {
                return i + 1;
            }
        } else {
            i += 1;
        }
    }
    bytes.len()
}

/// Where a `$tag$ ... $tag$` string starting at `i` ends; just past the `$` if it isn't one
/// (a `$1` parameter, say).
fn after_dollar(bytes: &[u8], i: usize) -> usize {
    let rest = &bytes[i + 1..];
    let n = rest
        .iter()
        .position(|&b| b == b'$' || !is_word_byte(b))
        .unwrap_or(rest.len());
    if rest.get(n) != Some(&b'$') || rest.first().is_some_and(u8::is_ascii_digit) {
        return i + 1;
    }
    let tag = &bytes[i..i + n + 2];
    let body = i + n + 2;
    bytes[body..]
        .windows(tag.len())
        .position(|w| w == tag)
        .map_or(bytes.len(), |p| body + p + tag.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn copy_from_or_to_the_client_is_spotted() {
        for sql in [
            "COPY t FROM STDIN",
            "copy t (a, b) from stdin with (format csv)",
            "COPY (SELECT 1) TO STDOUT",
            "  -- a comment\n /* another /* nested */ one */ Copy t To StdOut",
            "COPY BINARY t FROM STDIN",
            "SELECT 1; COPY t FROM STDIN; SELECT 2",
            "CREATE TABLE x (a int);\nCOPY x FROM stdin;",
            "COPY t FROM STDIN;",
            "COPY \"t\" FROM STDIN",
            "COPY (SELECT 'it''s', E'a\\'b', $$x$$, $q$y$q$) TO STDOUT",
            "COPY t (a, b) FROM STDIN",
            "COPY t FROM /* a comment */ STDIN",
            "COPY t FROM -- a comment\n STDIN",
        ] {
            assert!(copies_with_client(sql), "{sql}");
        }
    }

    #[test]
    fn everything_else_is_not() {
        for sql in [
            "",
            "SELECT 1",
            "SELECT stdin FROM t",
            "COPY t FROM '/tmp/stdin.csv'",
            "COPY t TO PROGRAM 'cat > /dev/stdout'",
            "COPY (SELECT 'stdout') TO '/tmp/x'",
            "COPY (SELECT E'\\' stdout') TO '/tmp/x'",
            "COPY \"stdin\" TO '/tmp/x'",
            "COPY (SELECT $$ STDOUT $$) TO '/tmp/x'",
            "COPY (SELECT $tag$ STDOUT $tag$) TO '/tmp/x'",
            "COPY t TO '/tmp/x' -- not STDOUT",
            "COPY t TO '/tmp/x' /* not STDOUT */",
            "COPY t TO '/tmp/x'; SELECT 'STDOUT'",
            "SELECT $1; SELECT 'COPY t FROM STDIN'",
            "INSERT INTO copy VALUES (1); SELECT stdin FROM t",
            "SELECT 'unterminated",
            "SELECT $$unterminated",
            "SELECT 1 /* unterminated",
            "COPY (SELECT stdin FROM t) TO '/tmp/x'",
            "COPY t (stdout) FROM '/tmp/f'",
            "COPY (SELECT 1 FROM stdin) TO '/tmp/x'",
            "COPY t FROM 'x' (FORMAT csv); SELECT stdin FROM t",
        ] {
            assert!(!copies_with_client(sql), "{sql}");
        }
    }
}
