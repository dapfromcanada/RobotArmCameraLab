# Security & Threat-Model Standards for AI Agents

> Cross-cutting. Apply to almost any project that stores data, talks to a network,
> or accepts user input. Skip only for a pure offline script with no sensitive
> data. Read this before writing code that touches input, auth, data, or secrets.

---

## 1. Quick threat model (do this once, up front)

Before coding, write 3–5 lines answering:

- **What are we protecting?** (user data, credentials, money, availability)
- **Who might attack, and how?** (malicious user input, a compromised dependency,
  a leaked secret, a network attacker)
- **Trust boundaries:** where does untrusted data cross into the system? (every
  request, file upload, env var, third-party response)

Everything below follows from treating data crossing a trust boundary as hostile.

## 2. Input validation & injection

- Validate and sanitize **all** external input against an explicit schema before
  use. Never trust client-supplied data — including headers, filenames, and IDs.
- Use parameterized queries / prepared statements for all database access. Never
  build SQL (or shell commands, or file paths) by string concatenation.
- Escape/encode output for its destination (HTML, SQL, shell, URL) to prevent
  XSS and injection.
- Reject oversized payloads and constrain types/ranges. Bound everything.

## 3. Secrets

- Never hard-code secrets, API keys, tokens, or connection strings. Use a `.env`
  file (git-ignored) or a secrets manager.
- Commit a `.env.example` with keys but no values.
- Never log secrets or full tokens. Scrub them from error messages and logs.
- Rotate any secret that may have been exposed; assume anything committed to git
  history is compromised.

## 4. Authentication & authorization

- Authenticate before authorizing; authorize **per resource** on every protected
  action, not just at login. (A logged-in user must not reach another user's data
  by changing an ID.)
- Store passwords only as salted hashes with a modern algorithm (bcrypt/argon2).
  Never store plaintext or reversible encryption for passwords.
- Use short-lived tokens with refresh where possible; store them securely
  (httpOnly cookies for web; OS keychain/secure storage for native/mobile).
- Enforce least privilege everywhere — DB accounts, API scopes, file permissions.

## 5. Data handling

- Encrypt sensitive data in transit (TLS everywhere) and at rest where required.
- Collect the minimum data needed; define retention and deletion. Honor PII rules
  (GDPR/HIPAA/etc.) if in scope.
- Never expose internal identifiers, stack traces, or system details in error
  responses sent to clients.

## 6. Dependencies & supply chain

- Prefer well-maintained libraries; check for known CVEs and recent activity
  before adopting one.
- Pin versions; run a vulnerability audit (`pip-audit`, `npm audit`, etc.) and
  address high/critical findings before a phase gate.
- Don't add a dependency for something trivial you can write safely yourself.

## 7. Safe defaults

- Fail closed: on error, deny access rather than allowing it.
- Set timeouts on all network calls; rate-limit public endpoints.
- Disable debug modes and verbose errors in production builds.
- Generate randomness/tokens with a cryptographically secure RNG, never `random()`.

## 8. Agent protocol for security-sensitive work

- Any change to auth, input parsing, data storage, secrets, or external calls is
  **security-sensitive**: do it at the strongest model tier (see
  `model-routing.md` — currently Claude Opus 5) and add a human review step
  before proceeding.
- If you find a vulnerability mid-task, STOP and report it before continuing.
- Never weaken a security control to make a test pass — fix the test or the design.

## 9. Pre-submit security checklist

- [ ] Threat model written (what we protect, who attacks, trust boundaries)
- [ ] All external input validated/sanitized against a schema
- [ ] Parameterized queries; no string-built SQL/shell/paths
- [ ] Output encoded for its destination (no XSS/injection)
- [ ] No hard-coded secrets; `.env` git-ignored; `.env.example` present
- [ ] Secrets never logged; tokens scrubbed from errors/logs
- [ ] Authz checked per resource; passwords hashed (bcrypt/argon2)
- [ ] TLS in transit; sensitive data encrypted at rest where required
- [ ] Dependencies audited; high/critical CVEs resolved
- [ ] Fails closed; secure RNG; debug/verbose errors off in production
