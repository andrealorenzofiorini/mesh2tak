#!/usr/bin/env bash
# Crea una PKI di prova come quella di TAK Server/infra-TAK:
# root CA -> CA intermedia (ca.pem) -> certificato server "takserver" e client "meshtastic-cnsas".
# Produce anche un pacchetto d'integrazione identico (nei nomi) a quello della console infra-TAK.
set -euo pipefail
D=${1:-/tmp/m2t-pki}
PASS=atakatak
rm -rf "$D"; mkdir -p "$D"; cd "$D"

cat > ext.cnf <<'EOF'
[ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
[server]
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=DNS:takserver
[client]
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
EOF

q() { "$@" >/dev/null 2>&1; }

q openssl req -x509 -newkey rsa:2048 -nodes -keyout root-ca.key -out root-ca.pem -days 30 -subj "/CN=root-ca/O=TAK" -addext basicConstraints=critical,CA:TRUE
q openssl req -newkey rsa:2048 -nodes -keyout ca.key -out ca.csr -subj "/CN=INT-CA-01/O=TAK"
q openssl x509 -req -in ca.csr -CA root-ca.pem -CAkey root-ca.key -CAcreateserial -out ca.pem -days 30 -extfile ext.cnf -extensions ca

q openssl req -newkey rsa:2048 -nodes -keyout takserver.key -out takserver.csr -subj "/CN=takserver/O=TAK"
q openssl x509 -req -in takserver.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out takserver.pem -days 30 -extfile ext.cnf -extensions server
cat ca.pem root-ca.pem > ca-chain.pem
cat takserver.pem ca.pem > takserver-chain.pem

# client del ponte: chiave cifrata con la password dei certificati TAK
q openssl req -newkey rsa:2048 -nodes -keyout mc-plain.key -out mc.csr -subj "/CN=meshtastic-cnsas/O=TAK"
q openssl x509 -req -in mc.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out meshtastic-cnsas.pem -days 30 -extfile ext.cnf -extensions client
q openssl pkey -in mc-plain.key -aes256 -passout pass:$PASS -out meshtastic-cnsas.key
q openssl pkcs12 -export -in meshtastic-cnsas.pem -inkey mc-plain.key -certfile ca-chain.pem -name meshtastic-cnsas -passout pass:$PASS -out meshtastic-cnsas.p12

# client firmato da una CA estranea (deve essere rifiutato)
q openssl req -x509 -newkey rsa:2048 -nodes -keyout rogue.key -out rogue.pem -days 30 -subj "/CN=rogue"

# CA estranea per provare la verifica del server
q openssl req -x509 -newkey rsa:2048 -nodes -keyout other-ca.key -out other-ca.pem -days 30 -subj "/CN=other-ca" -addext basicConstraints=critical,CA:TRUE

mkdir -p pkg
cp meshtastic-cnsas.p12 meshtastic-cnsas.pem meshtastic-cnsas.key ca-chain.pem pkg/
cat > pkg/README.txt <<EOF
TAK Server integration package: meshtastic-cnsas
Created 2026-11-01 15:00 UTC by infra-TAK v10.3.3-alpha

Server:  ${SERVER_NAME:-localhost}
Port:    8089 (TLS, client certificate required)

Files:
  meshtastic-cnsas.p12  Client certificate + private key (PKCS#12 / .pfx, AES-256).
  meshtastic-cnsas.pem  Client certificate (PEM).
  meshtastic-cnsas.key  Client private key (PEM, encrypted).
  ca-chain.pem          This server's certificate authority (PEM).

Password: the .p12 files and the .key are protected with the TAK certificate password.
It is not included in this package; get it from your TAK Server administrator.
EOF
(cd pkg && zip -q ../meshtastic-cnsas-integration.zip *)
openssl pkey -in meshtastic-cnsas.key -passin pass:$PASS -out meshtastic-cnsas-plain.key 2>/dev/null
echo "PKI pronta in $D"
