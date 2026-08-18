//! Micro pattern matcher for the `pattern` JSON-Schema keyword — no regex
//! crate. Supports exactly the constructs the STAMMTISCH contracts (and the
//! doctrine domain schemas) use:
//!
//! - anchored patterns only (`^...$`)
//! - character classes `[a-z0-9-]` (ranges and literals)
//! - literal characters (`-`, `:`, `.`, alphanumerics, …)
//! - backslash escaping of the next character
//! - counted repetition `{n}`, `{m,n}`, `{m,}`, plus `*`, `+`, `?`
//!
//! Anything else (alternation, groups, lookahead, …) is rejected with an
//! error, and validation fails closed — never a silent best-effort match.

#[derive(Debug, Clone)]
enum Atom {
    Lit(char),
    Class(Vec<(char, char)>),
}

#[derive(Debug, Clone)]
struct Piece {
    atom: Atom,
    min: usize,
    max: usize, // usize::MAX = unbounded
}

pub fn matches(pattern: &str, text: &str) -> Result<bool, String> {
    let inner = pattern
        .strip_prefix('^')
        .and_then(|p| p.strip_suffix('$'))
        .ok_or_else(|| format!("unsupported pattern (must be ^...$ anchored): {pattern}"))?;
    let pieces = parse(inner)?;
    let chars: Vec<char> = text.chars().collect();
    Ok(match_from(&pieces, 0, &chars, 0))
}

fn parse(s: &str) -> Result<Vec<Piece>, String> {
    let chars: Vec<char> = s.chars().collect();
    let mut i = 0;
    let mut out = Vec::new();
    while i < chars.len() {
        let atom = match chars[i] {
            '[' => {
                i += 1;
                let mut ranges = Vec::new();
                let mut first = true;
                loop {
                    if i >= chars.len() {
                        return Err("unterminated character class".into());
                    }
                    if chars[i] == ']' && !first {
                        i += 1;
                        break;
                    }
                    first = false;
                    let lo = if chars[i] == '\\' {
                        i += 1;
                        *chars
                            .get(i)
                            .ok_or_else(|| "dangling escape in class".to_string())?
                    } else {
                        chars[i]
                    };
                    i += 1;
                    let hi = if i + 1 < chars.len() && chars[i] == '-' && chars[i + 1] != ']' {
                        let h = chars[i + 1];
                        i += 2;
                        h
                    } else {
                        lo
                    };
                    if hi < lo {
                        return Err(format!("inverted class range {lo}-{hi}"));
                    }
                    ranges.push((lo, hi));
                }
                Atom::Class(ranges)
            }
            '.' => {
                // Treat a bare dot as a literal dot: every pattern we accept
                // uses dots literally, and full any-semantics invites drift.
                i += 1;
                Atom::Lit('.')
            }
            '\\' => {
                let c = *chars
                    .get(i + 1)
                    .ok_or_else(|| "dangling escape".to_string())?;
                i += 2;
                Atom::Lit(c)
            }
            '(' | ')' | '|' => return Err(format!("unsupported pattern construct '{}'", chars[i])),
            c => {
                i += 1;
                Atom::Lit(c)
            }
        };
        let (min, max) = if i < chars.len() {
            match chars[i] {
                '{' => {
                    let end = chars[i..]
                        .iter()
                        .position(|&c| c == '}')
                        .map(|p| i + p)
                        .ok_or_else(|| "unterminated quantifier".to_string())?;
                    let body: String = chars[i + 1..end].iter().collect();
                    i = end + 1;
                    parse_quantifier(&body)?
                }
                '*' => {
                    i += 1;
                    (0, usize::MAX)
                }
                '+' => {
                    i += 1;
                    (1, usize::MAX)
                }
                '?' => {
                    i += 1;
                    (0, 1)
                }
                _ => (1, 1),
            }
        } else {
            (1, 1)
        };
        out.push(Piece { atom, min, max });
    }
    Ok(out)
}

fn parse_quantifier(body: &str) -> Result<(usize, usize), String> {
    let parts: Vec<&str> = body.split(',').collect();
    let num = |s: &str| {
        s.parse::<usize>()
            .map_err(|_| format!("bad quantifier {{{body}}}"))
    };
    match parts.as_slice() {
        [n] => Ok((num(n)?, num(n)?)),
        [m, ""] => Ok((num(m)?, usize::MAX)),
        [m, n] => {
            let (m, n) = (num(m)?, num(n)?);
            if n < m {
                return Err(format!("bad quantifier {{{body}}}"));
            }
            Ok((m, n))
        }
        _ => Err(format!("bad quantifier {{{body}}}")),
    }
}

fn atom_matches(atom: &Atom, c: char) -> bool {
    match atom {
        Atom::Lit(l) => *l == c,
        Atom::Class(ranges) => ranges.iter().any(|(lo, hi)| *lo <= c && c <= *hi),
    }
}

/// Greedy with backtracking; piece counts are tiny in contract patterns.
fn match_from(pieces: &[Piece], pi: usize, chars: &[char], ci: usize) -> bool {
    if pi == pieces.len() {
        return ci == chars.len();
    }
    let piece = &pieces[pi];
    let mut consumed = 0;
    while consumed < piece.max
        && ci + consumed < chars.len()
        && atom_matches(&piece.atom, chars[ci + consumed])
    {
        consumed += 1;
    }
    loop {
        if consumed >= piece.min && match_from(pieces, pi + 1, chars, ci + consumed) {
            return true;
        }
        if consumed == 0 {
            return false;
        }
        consumed -= 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(p: &str, t: &str) -> bool {
        matches(p, t).unwrap()
    }

    #[test]
    fn kebab_id_pattern() {
        let p = "^[a-z0-9][a-z0-9-]{1,63}$";
        assert!(ok(p, "stock-daily"));
        assert!(ok(p, "a1"));
        assert!(!ok(p, "a")); // needs 2+
        assert!(!ok(p, "-lead"));
        assert!(!ok(p, "Upper"));
        assert!(!ok(p, "has space"));
    }

    #[test]
    fn sha256_pattern() {
        let p = "^sha256:[0-9a-f]{64}$";
        assert!(ok(p, &format!("sha256:{}", "a".repeat(64))));
        assert!(ok(p, &format!("sha256:{}", "9f".repeat(32))));
        assert!(!ok(p, &format!("sha256:{}", "g".repeat(64))));
        assert!(!ok(p, &format!("sha256:{}", "a".repeat(63))));
    }

    #[test]
    fn uuid_v7_pattern() {
        let p = "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$";
        assert!(ok(p, "019b4e5a-2c3d-7e8f-9a0b-1c2d3e4f5a6b"));
        assert!(!ok(p, "019b4e5a-2c3d-6e8f-9a0b-1c2d3e4f5a6b")); // version nibble
        assert!(!ok(p, "019b4e5a-2c3d-7e8f-7a0b-1c2d3e4f5a6b")); // variant nibble
    }

    #[test]
    fn backtracking() {
        assert!(ok("^[a-z]{2,4}-[0-9]+$", "ab-1"));
        assert!(ok("^[a-z]{2,4}-[0-9]+$", "abcd-999"));
        assert!(!ok("^[a-z]{2,4}-[0-9]+$", "abcde-1"));
    }

    #[test]
    fn rejects_unsupported_constructs() {
        assert!(matches("^(a|b)$", "a").is_err());
        assert!(matches("a+$", "aa").is_err()); // unanchored
        assert!(matches("^a{2,1}$", "aa").is_err());
    }
}
