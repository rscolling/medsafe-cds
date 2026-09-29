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
