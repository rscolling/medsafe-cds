# Vendored: CivicActions/vista-clients

- Upstream: https://github.com/CivicActions/vista-clients (v0.1.0, commit b828336, Apache-2.0,
  Copyright 2025 Owen Barton). Only the `vista_clients.rpc` sub-package is vendored (the terminal
  sub-package is not needed). Upstream `LICENSE` is kept alongside in this directory.
- NOT used: `vavista-rpc` (AGPL-3.0).

## Local modifications (fork), all marked `medsafe-cds patch` in source

1. `rpc/transport.py`: `Transport.receive()` records `last_reply_had_null_prefix` (whether the
   `\x00\x00` success prefix was present).
2. `rpc/protocol.py`: `parse_response(raw, *, had_null_prefix=None)`. Upstream treated any reply whose
   first byte is < 0x20 as a SNDERR length prefix, so a valid data reply such as `"\r\nNo Data Found"`
   raised a truncated `RPCError('\nNo Data Foun')`. When the transport saw the success prefix the payload
   is now always treated as data.
3. `rpc/broker.py`: passes that flag to `parse_response`.
4. `rpc/broker.py`: removed the built-in fallback demo credentials. `_resolve_credentials` now raises
   `AuthenticationError` unless codes are passed explicitly or set via `VISTA_ACCESS_CODE` / `VISTA_VERIFY_CODE`,
   so no credential is hard-coded anywhere in this repository (the public VEHU demo pair lives only in `.env.example`).
5. `rpc/protocol.py`: the M-error / "Remote Procedure ... doesn't exist" detection now also runs when the reply starts with
   a one-byte SNDERR length prefix (control character such as `\x18`, or `=` / `>`), so these errors are raised as
   `RPCError` instead of being returned as data (which had made VistA errors look like empty meds/labs). Verified
   against live VEHU replies; the `"\r\nNo Data Found"` data fix is unchanged.
6. `rpc/broker.py`: removed the unused `_redact` helper and its `_REDACT_RE` (upstream defined them but never
   called them, so they gave a false impression that broker log lines were redacted). The application does its own
   redaction (`app/logging_setup.py`); the vendored library logs only sizes, states and the DUZ, never the codes.
