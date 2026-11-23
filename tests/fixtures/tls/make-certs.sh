#!/usr/bin/env bash
# TEST-ONLY key material for the name smtp.test. These keys protect nothing: they are
# committed on purpose, and the tests alone trust them (through an explicit `cadata` context).
# Run from this directory: ./make-certs.sh. Needs OpenSSL 3.
set -euo pipefail

cat > openssl.cnf <<'EOF'
[req]
distinguished_name = dn
prompt = no
[dn]
CN = earmark test CA
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
[v3_srv]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:smtp.test
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
EOF
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout ca.key \
  -out ca.pem -days 36500 -config openssl.cnf -extensions v3_ca
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout server.key \
  -out server.csr -subj "/CN=smtp.test"
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out server.pem -days 36500 -extfile openssl.cnf -extensions v3_srv
rm -f ca.key server.csr ca.srl openssl.cnf
