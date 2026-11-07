#!/bin/sh
# One-time private certificate enrollment for the Pi Wi-Fi dashboard.
set -eu
umask 077
if [ "$#" -ne 2 ]; then
    echo "Usage: sudo $0 <pi-mdns-name> <pi-wifi-ip>" >&2
    exit 2
fi
dns_name=$1
wifi_ip=$2
case "$dns_name" in *[!a-zA-Z0-9.-]*|'') echo 'Invalid DNS name' >&2; exit 2;; esac
case "$wifi_ip" in *[!0-9.]*|'') echo 'Invalid IPv4 address' >&2; exit 2;; esac
cert_dir=/etc/llm-gateway/lan
enroll_dir=/home/bryan/.local/share/llm-gateway/enrollment
if [ -e "$cert_dir/ca.key" ] || [ -e "$cert_dir/server.key" ]; then
    echo 'Certificate files already exist; refusing to replace them' >&2
    exit 1
fi
install -d -m 750 -o root -g bryan "$cert_dir"
install -d -m 700 -o bryan -g bryan "$enroll_dir"

openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
    -subj '/CN=Pi Dashboard Local CA' \
    -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -keyout "$cert_dir/ca.key" -out "$cert_dir/ca.crt" >/dev/null 2>&1
openssl req -new -newkey rsa:3072 -sha256 -nodes \
    -subj "/CN=$dns_name" \
    -keyout "$cert_dir/server.key" -out "$cert_dir/server.csr" >/dev/null 2>&1
cat > "$cert_dir/server.ext" <<EOF
subjectAltName=DNS:$dns_name,IP:$wifi_ip
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF
openssl x509 -req -in "$cert_dir/server.csr" -CA "$cert_dir/ca.crt" \
    -CAkey "$cert_dir/ca.key" -CAcreateserial -days 365 -sha256 \
    -extfile "$cert_dir/server.ext" -out "$cert_dir/server.crt" >/dev/null 2>&1

openssl req -new -newkey rsa:3072 -sha256 -nodes \
    -subj '/CN=bryan-desktop' \
    -keyout "$cert_dir/desktop.key" -out "$cert_dir/desktop.csr" >/dev/null 2>&1
cat > "$cert_dir/client.ext" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=clientAuth
EOF
openssl x509 -req -in "$cert_dir/desktop.csr" -CA "$cert_dir/ca.crt" \
    -CAkey "$cert_dir/ca.key" -CAcreateserial -days 365 -sha256 \
    -extfile "$cert_dir/client.ext" -out "$cert_dir/desktop.crt" >/dev/null 2>&1
openssl rand -hex 24 > "$enroll_dir/desktop.p12.password"
openssl pkcs12 -export -inkey "$cert_dir/desktop.key" -in "$cert_dir/desktop.crt" \
    -certfile "$cert_dir/ca.crt" -out "$enroll_dir/desktop.p12" \
    -passout "file:$enroll_dir/desktop.p12.password" >/dev/null 2>&1
cp "$cert_dir/ca.crt" "$enroll_dir/ca.crt"
chown bryan:bryan "$enroll_dir/desktop.p12" "$enroll_dir/desktop.p12.password" "$enroll_dir/ca.crt"
chmod 600 "$enroll_dir/desktop.p12" "$enroll_dir/desktop.p12.password"
chown root:bryan "$cert_dir/server.key"
chmod 640 "$cert_dir/server.key"
chmod 600 "$cert_dir/ca.key"
chmod 644 "$cert_dir/ca.crt" "$cert_dir/server.crt"
printf 'LAN_DASHBOARD_BIND=%s\nLAN_DASHBOARD_ALLOWED_HOSTS=%s\n' "$wifi_ip" "$dns_name" > "$cert_dir/environment"
chown root:bryan "$cert_dir/environment"
chmod 640 "$cert_dir/environment"
rm -f "$cert_dir/server.csr" "$cert_dir/server.ext" "$cert_dir/desktop.key" "$cert_dir/desktop.csr" "$cert_dir/client.ext" "$cert_dir/ca.srl"
echo 'Created server and client certificates; enrollment files are ready for the desktop.'
