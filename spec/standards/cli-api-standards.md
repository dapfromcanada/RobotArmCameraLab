# CLI, API & Service Standards for AI Agents

> Scope: command-line tools, HTTP/RPC APIs, and long-running background services.
> Read this at the start of the relevant phase before writing code. Apply
> `security-standards.md` alongside it for anything network- or data-facing.

---

## A. Shared structure (all three)

- Separate layers: a thin entry/interface layer (CLI parser, HTTP router, or
  service loop) that calls a **core logic layer** which is independent of how it
  was invoked. The core must be unit-testable without a terminal, server, or
  scheduler.
- Configuration comes from explicit flags/args, then environment variables, then
  a config file, then defaults — in that precedence order. Document it.
- Secrets never come from flags (they leak into shell history/process lists) —
  use env vars or a secrets manager.
- Structured logging (level + message + context fields). No `print` for
  diagnostics in anything long-lived. Logs go to stderr; real output to stdout.
- Fail loudly and specifically: a non-zero exit / proper error response with an
  actionable message. Never swallow errors silently.

## B. CLI tools

- Use an arg-parsing library (argparse/click/typer, commander, cobra) — never
  hand-roll `sys.argv` parsing.
- Provide `--help` for the tool and every subcommand, with examples.
- Support `--version`, `--quiet`, and `--json` (machine-readable output) where
  sensible.
- Exit codes are meaningful: 0 success, non-zero per failure class. Document them.
- Read from stdin and write to stdout so the tool composes in pipes. Progress and
  prompts go to stderr.
- Long operations show progress; destructive operations require confirmation or
  an explicit `--force`.

## C. APIs (HTTP / RPC)

- Validate every input (body, query, path, headers) against a schema before use.
  Reject invalid input with a clear 4xx and a consistent error shape
  `{ error: { code, message } }`.
- Version the API (`/v1/…`) from day one so breaking changes don't break clients.
- Use correct status codes and idempotent methods where the semantics call for it
  (GET safe, PUT/DELETE idempotent).
- Paginate and bound all list endpoints. Never return unbounded result sets.
- Authentication and authorization on every non-public endpoint; check authz per
  resource, not just authn. (See `security-standards.md`.)
- Rate-limit public endpoints. Set timeouts on every outbound call.
- Document endpoints (OpenAPI/schema). Keep the doc generated from code where
  possible so it can't drift.

## D. Background services / daemons

- Idempotent work: a job that runs twice must not corrupt state (use keys,
  transactions, or dedupe).
- Graceful shutdown: handle SIGTERM/SIGINT, finish or safely abandon in-flight
  work, release resources.
- Health/readiness signal (endpoint, heartbeat, or log) so the supervisor knows
  it's alive.
- Retries with backoff for transient failures; a dead-letter path for permanent
  ones. Never retry forever in a tight loop.
- Make the run interval/trigger configurable; never hard-code schedules.

## E. Testing

- Unit-test the core logic layer directly (no server/CLI needed).
- Integration-test the interface layer: CLI invocations with sample args; API
  endpoints with a test client; service handlers with simulated triggers.
- Test error and edge paths, not just the happy path. All tests pass at a gate.

## F. Agent modification protocol

1. Change the core logic layer and its unit tests together; keep the interface
   layer thin.
2. Never put business logic in a CLI parser, route handler, or service loop.
3. Run unit tests after every change; run integration tests before a phase gate.
4. Any change to auth, input parsing, or external calls also applies
   `security-standards.md` and gets a human review step.

## G. Pre-submit checklist

- [ ] Core logic layer is independent and unit-testable in isolation
- [ ] Inputs validated against a schema; clear errors on bad input
- [ ] Config precedence (flags > env > file > defaults) documented
- [ ] Secrets via env/secrets manager, never flags or hard-coded
- [ ] Structured logging to stderr; real output to stdout
- [ ] CLI: `--help`/`--version`, meaningful exit codes, pipe-friendly
- [ ] API: versioned, paginated, authn+authz, rate-limited, documented
- [ ] Service: idempotent, graceful shutdown, health signal, backoff retries
- [ ] Tests cover error paths and pass
