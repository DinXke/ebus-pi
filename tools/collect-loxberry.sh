#!/bin/bash
# Vooraf, vanaf je pc (Git Bash), terwijl de LoxBerry nog draait: alles ophalen wat de nieuwe kaart nodig heeft.
# Er wordt op de LoxBerry NIETS gewijzigd. Je typt zelf het SSH-wachtwoord van de LoxBerry.
#
#   bash tools/collect-loxberry.sh [loxberry@192.168.255.3] [doelmap]
#
# Resultaat (standaard ./loxberry-overname, NIET in git: bevat het MQTT-wachtwoord):
#   ebusd-etc.tgz   /etc/ebusd + /etc/default/ebusd (+ pluginlijst en pluginconfig, bv. Netatmo)
#   setmode.txt     huidige SetMode van de ketel (bv. auto;30.0;45.0;65;0;0;0;0;0;0)
#   ebusd-info.txt  ebusd 'info' (versie, adapter, ketel-ID)
# Kopieer de map naar de bootpartitie van de nieuwe SD-kaart (bootfs/loxberry-overname).
set -euo pipefail
FROM="${1:-loxberry@192.168.255.3}"
OUT="${2:-loxberry-overname}"
HOST="${FROM#*@}"
mkdir -p "$OUT"

echo "== ebusd-config en pluginlijst ophalen via SSH (wachtwoord van $FROM)"
ssh -o StrictHostKeyChecking=accept-new "$FROM" \
	"tar -C / -czf - etc/ebusd etc/default/ebusd \$(ls -d opt/loxberry/data/system/plugindatabase.json* opt/loxberry/config/plugins 2>/dev/null) 2>/dev/null" \
	> "$OUT/ebusd-etc.tgz"
echo "   $(tar -tzf "$OUT/ebusd-etc.tgz" | wc -l) bestanden"

echo "== SetMode en info lezen van ebusd op $HOST:8888 (alleen lezen)"
PY=$(command -v python3 || command -v python)
"$PY" - "$HOST" "$OUT" <<'PY'
import socket, sys
host, out = sys.argv[1], sys.argv[2]
s = socket.create_connection((host, 8888), timeout=5)
s.settimeout(10)
def q(cmd):
    s.sendall((cmd + "\n").encode())
    buf = b""
    while b"\n\n" not in buf:
        c = s.recv(65536)
        if not c:
            break
        buf += c
    return buf.decode("utf-8", "replace").strip()
sm = q("read -c bai SetMode")
open(f"{out}/setmode.txt", "w").write(sm + "\n")
open(f"{out}/ebusd-info.txt", "w").write(q("info") + "\n")
print("   SetMode:", sm)
PY

echo "== pluginlijst"
tar -xzOf "$OUT/ebusd-etc.tgz" opt/loxberry/data/system/plugindatabase.json 2>/dev/null \
	| "$PY" -c "import json,sys; d=json.load(sys.stdin); [print('  -', p.get('title') or p.get('name'), p.get('version','')) for p in (d.get('plugins') or {}).values()]" \
	|| echo "   (niet gevonden; kijk in de LoxBerry onder Plugin-beheer)"
echo "Klaar: $OUT"
