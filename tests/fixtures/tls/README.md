# Test TLS material

`ca.pem`, `server.pem` and `server.key` are **test-only** and protect nothing. They are
committed on purpose so the SMTP stub in `tests/smtp_stub.py` can speak real TLS without
generating certificates at test time.

- The server certificate is for the fake host name `smtp.test` only (CN and SAN). It is
  never a real host.
- The CA key was deleted when they were made, so nothing else can be signed by this CA.
- Only the tests trust the CA, through an explicit `cadata` context. Nothing installs it
  into a system trust store, and no earmark setting points at it.
- Regenerate with `./make-certs.sh` (OpenSSL 3). Validity is 36500 days.

Do not reuse these files for anything real.
