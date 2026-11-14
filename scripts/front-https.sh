#!/bin/sh
# Serve the control node's console over HTTPS on the home network, with no outside service,
# so browsers give its pages the microphone. Run it on compute, from anywhere:
#
#   scripts/front-https.sh
#
# A home certificate authority (CA) on compute signs a certificate for the control node.
# nginx there answers https://front.home.arpa and passes it to the console on 127.0.0.1:8440.
# The CA's key never leaves compute: whoever holds it can make certificates every trusting
# device accepts, though only for *.home.arpa and LAN addresses. Each phone and computer
# must trust CA_DIR/home-ca.crt once; the script says how when it ends.
#
# Once HTTPS answers, the console stops listening on the LAN in plain HTTP and its sign-in
# cookie travels only over HTTPS (KEEP_HTTP=1 leaves the console as it is). Run the script
# again before the certificate runs out: it signs a new one 30 days ahead and reloads nginx.
# It needs sudo without a password on the control node.
set -eu

FRONT="${FRONT:-10.77.10.42}"            # the control node, as ssh reaches it
IP="${IP:-$FRONT}"                       # its LAN address; reserve it in the router so it never moves
NAME="${NAME:-front.home.arpa}"          # the name the router's DNS gives that address
LAN="${LAN:-10.77.10.0/255.255.255.0}"   # the addresses the CA may sign for, beside *.home.arpa
WHERE="${WHERE:-weightroom}"             # the console's folder there, under the home folder
CA_DIR="${CA_DIR:-$HOME/ca}"
case "$NAME" in *.home.arpa) ;; *) echo 'NAME must end in .home.arpa: the CA may sign nothing else'; exit 2;; esac
case "$WHERE" in *[!a-zA-Z0-9_/-]*|/*|*..*) echo 'WHERE must be a simple relative folder'; exit 2;; esac

mkdir -p "$CA_DIR"
cd "$CA_DIR"
umask 077
if [ ! -f home-ca.key ]; then
  echo "Making the home CA in $CA_DIR"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
    -keyout home-ca.key -out home-ca.crt -days 3650 -subj "/CN=Home LAN CA" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "nameConstraints=critical,permitted;DNS:home.arpa,permitted;IP:$LAN"
fi
# 825 days is the longest Apple devices accept from a CA someone added themselves.
if ! openssl x509 -in "$NAME.crt" -noout -checkend 2592000 >/dev/null 2>&1; then
  echo "Signing a certificate for $NAME and $IP"
  printf 'subjectAltName=DNS:%s,IP:%s\nextendedKeyUsage=serverAuth\n' "$NAME" "$IP" > "$NAME.ext"
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$NAME.key" -out "$NAME.csr" -subj "/CN=$NAME"
  openssl x509 -req -in "$NAME.csr" -CA home-ca.crt -CAkey home-ca.key -CAcreateserial \
    -out "$NAME.crt" -days 825 -extfile "$NAME.ext"
  rm "$NAME.csr" "$NAME.ext"
fi

echo "Putting nginx and the certificate on $FRONT"
ssh "$FRONT" 'dpkg -s nginx >/dev/null 2>&1 || { sudo apt-get update && sudo apt-get install -y nginx; }
              sudo install -d -m 700 /etc/nginx/tls'
ssh "$FRONT" "sudo sh -c 'umask 077; cat > /etc/nginx/tls/$NAME.key'" < "$NAME.key"
ssh "$FRONT" "sudo sh -c 'cat > /etc/nginx/tls/$NAME.crt'" < "$NAME.crt"
# A site that fails nginx's check is taken out again, so a later restart or reboot still starts nginx.
ssh "$FRONT" "sudo tee /etc/nginx/sites-available/front-tls >/dev/null
              sudo ln -sf ../sites-available/front-tls /etc/nginx/sites-enabled/front-tls
              sudo nginx -t || { sudo rm /etc/nginx/sites-enabled/front-tls; exit 1; }
              sudo systemctl reload-or-restart nginx" <<EOF
# Written by scripts/front-https.sh on compute. Run that again rather than editing this.
server {
    listen $IP:443 ssl;   # the LAN address only: Tailscale Serve holds 443 on its own address
    server_name $NAME;
    ssl_certificate     /etc/nginx/tls/$NAME.crt;
    ssl_certificate_key /etc/nginx/tls/$NAME.key;
    client_max_body_size 200m;   # audio and image uploads; nginx allows 1m by default

    location / {
        proxy_pass http://127.0.0.1:8440;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;                   # the console compares a form's Origin with Host
        proxy_set_header X-Forwarded-For \$remote_addr; # uvicorn believes these from 127.0.0.1 only,
        proxy_set_header X-Forwarded-Proto https;       # so the console logs the real address
        proxy_buffering off;                            # streamed chat answers
        proxy_read_timeout 1h;
    }
}
EOF

echo "Checking https://$NAME from here"
curl -fsS -o /dev/null --cacert home-ca.crt --resolve "$NAME:443:$IP" "https://$NAME/"

if [ "${KEEP_HTTP:-0}" != 1 ]; then
  ssh "$FRONT" "python3 - $WHERE/settings/weightroom.toml" <<'EOF'
# Only the top-level host and secure change: the result is checked against the file as it was.
import pathlib, re, shutil, subprocess, sys, tomllib
path = pathlib.Path(sys.argv[1])
text = path.read_text()
top, sep, rest = text.partition("\n[")   # the top-level keys end where the first table starts
top = re.sub(r'(?m)^host\s*=.*$', 'host = "127.0.0.1"', top)
if re.search(r'(?m)^secure\s*=', top):
    top = re.sub(r'(?m)^secure\s*=.*$', 'secure = true', top)
else:
    top = top.rstrip("\n") + "\nsecure = true\n"
new = top + sep + rest
if tomllib.loads(new) != {**tomllib.loads(text), "host": "127.0.0.1", "secure": True}:
    sys.exit(f"{path} is not shaped as expected; set host = \"127.0.0.1\" and secure = true in it by hand")
if new != text:
    shutil.copy(path, path.with_name(path.name + ".before-https"))
    path.write_text(new)
    subprocess.run(["systemctl", "--user", "restart", "weightroom.service"], check=True)
    print("Console now listens on 127.0.0.1 only and keeps its cookie to HTTPS; the old settings are in", path.name + ".before-https")
EOF
fi

cat <<EOF

Done: https://$NAME (and https://$IP) reach the console.
Still to do by hand:
  - Router DNS: an A record $NAME -> $IP, and a fixed address for the control node.
  - Trust $CA_DIR/home-ca.crt once on each device (the .crt only, never the .key):
      iPhone:  AirDrop it, Settings > Profile Downloaded > Install,
               then General > About > Certificate Trust Settings > turn it on
      Android: Settings > Security > Encryption & credentials > Install a certificate > CA certificate
      Mac:     open it in Keychain Access (System), set Trust to Always Trust
      Chrome on Linux: chrome://certificate-manager > Local certificates > Custom > Import
      Firefox: Settings > Certificates > View Certificates > Authorities > Import
EOF
