#!/bin/bash
# ebus-pi installeren op een kale Raspberry Pi OS Lite (64-bit, Debian 12/13).
#
#   sudo bash install.sh                                   alles installeren, vraagt de wachtwoorden
#   sudo bash install.sh --from-dir /boot/firmware/loxberry-overname
#                                                          + ebusd-config, opties, MQTT-wachtwoord en
#                                                            SetMode-beginwaarden uit de map die
#                                                            tools/collect-loxberry.sh vooraf maakte
#   sudo bash install.sh --from-loxberry loxberry@192.168.255.3
#                                                          idem, rechtstreeks (enkel als de LoxBerry
#                                                            nog draait, bv. op een tweede Pi)
# Opties: --hostname NAAM (standaard ebus-pi) · --ebusd-open (ebusd-poort 8888 ook op het netwerk)
#         --ebusd-version 26.1 (standaard: dezelfde als de LoxBerry nu) · --update (enkel de code bijwerken)
#
# Wachtwoorden worden hier gevraagd en enkel lokaal bewaard (/etc/ebuspi, rechten 600), nooit in de repo.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FROM=""; FROM_DIR=""; HOSTNAME_NEW="ebus-pi"; EBUSD_OPEN=0; EBUSD_VERSION="26.1"; ONLY_UPDATE=0
while [ $# -gt 0 ]; do
	case "$1" in
	--from-loxberry) FROM="$2"; shift 2 ;;
	--from-dir) FROM_DIR="$2"; shift 2 ;;
	--hostname) HOSTNAME_NEW="$2"; shift 2 ;;
	--ebusd-open) EBUSD_OPEN=1; shift ;;
	--ebusd-version) EBUSD_VERSION="$2"; shift 2 ;;
	--update) ONLY_UPDATE=1; shift ;;
	*) echo "onbekende optie $1"; exit 2 ;;
	esac
done
[ "$(id -u)" = 0 ] || { echo "start met sudo"; exit 1; }

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
VER="$(python3 -c "import sys; sys.path.insert(0, '$SRC'); import ebuspi; print(ebuspi.VERSION)")"

# ---------------------------------------------------------------- code
install_code() {
	say "ebus-pi $VER naar /opt/ebuspi"
	id ebuspi >/dev/null 2>&1 || useradd --system --home /var/lib/ebuspi --shell /usr/sbin/nologin ebuspi
	usermod -aG systemd-journal ebuspi
	install -d -m 755 /opt/ebuspi/releases
	rm -rf "/opt/ebuspi/releases/$VER"
	install -d -m 755 "/opt/ebuspi/releases/$VER"
	cp -r "$SRC/ebuspi" "$SRC/system" "$SRC/README.md" "/opt/ebuspi/releases/$VER/"
	find "/opt/ebuspi/releases/$VER" -name __pycache__ -prune -exec rm -rf {} +
	chown -R root:root "/opt/ebuspi/releases/$VER"
	ln -sfn "/opt/ebuspi/releases/$VER" /opt/ebuspi/current
	install -m 755 "$SRC/system/ebuspi-helper" /usr/local/sbin/ebuspi-helper
	install -m 755 "$SRC/system/install-ebusd.sh" /usr/local/sbin/ebuspi-install-ebusd
}

if [ "$ONLY_UPDATE" = 1 ]; then
	install_code
	systemctl restart ebuspi
	say "bijgewerkt naar $VER"
	exit 0
fi

# ---------------------------------------------------------------- pakketten
say "pakketten"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get -y install python3 python3-paho-mqtt network-manager iw curl \
	unattended-upgrades ca-certificates openssh-client
if ! ebusd --version 2>/dev/null | grep -q "ebusd ${EBUSD_VERSION}"; then
	say "ebusd ${EBUSD_VERSION} (GitHub-release)"
	EBUSD_VERSION="$EBUSD_VERSION" bash "$SRC/system/install-ebusd.sh"
fi

install_code

# ---------------------------------------------------------------- overnemen van de LoxBerry (alleen lezen)
OLD_OPTS=""; OLD_MQTTPASS=""
SEED=""
if [ -n "$FROM" ] || [ -n "$FROM_DIR" ]; then
	TMP="$(mktemp -d)"
	if [ -n "$FROM_DIR" ]; then
		say "overnemen uit $FROM_DIR"
		tar -C "$TMP" -xzf "$FROM_DIR/ebusd-etc.tgz"
		[ -f "$FROM_DIR/setmode.txt" ] && SEED="$(head -n 1 "$FROM_DIR/setmode.txt" | tr -d '\r')"
	else
		say "overnemen van $FROM (je SSH-wachtwoord van de LoxBerry wordt gevraagd; er wordt daar niets gewijzigd)"
		# één verbinding: /etc/ebusd en /etc/default/ebusd als tar naar hier
		ssh -o StrictHostKeyChecking=accept-new "$FROM" "tar -C / -czf - etc/ebusd etc/default/ebusd 2>/dev/null" \
			| tar -C "$TMP" -xzf -
	fi
	if [ -d "$TMP/etc/ebusd" ]; then
		[ -d /etc/ebusd ] && cp -a /etc/ebusd "/etc/ebusd.voor-overname.$(date +%s)"
		rm -rf /etc/ebusd && cp -a "$TMP/etc/ebusd" /etc/ebusd
		chown -R root:root /etc/ebusd
		echo "ebusd-config overgenomen ($(find /etc/ebusd -name '*.csv' | wc -l) csv-bestanden)"
	fi
	if [ -f "$TMP/etc/default/ebusd" ]; then
		OLD_OPTS="$(sed -n 's/^EBUSD_OPTS="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$TMP/etc/default/ebusd" | head -n 1)"
		OLD_MQTTPASS="$(echo "$OLD_OPTS" | grep -oP -- '--mqttpass[= ]\K[^ ]+' || true)"
		echo "oude ebusd-opties gevonden${OLD_MQTTPASS:+ (met MQTT-wachtwoord, dat apart bewaard wordt)}"
	fi
	rm -rf "$TMP"
fi

# ---------------------------------------------------------------- ebusd-opties
say "ebusd-opties"
if [ -n "$OLD_OPTS" ]; then
	# zelfde opties als de LoxBerry, zonder wachtwoord en zonder logbestand (log gaat naar het journal)
	OPTS="$(echo " $OLD_OPTS " | sed -E 's/ --mqttpass[= ][^ ]+//; s/ --logfile[= ][^ ]+//; s/ (-f|--foreground)( |$)/ /; s/  +/ /g; s/^ //; s/ $//')"
else
	OPTS="-d /dev/ttyUSB0 --scanconfig --configpath=/etc/ebusd --accesslevel=* --latency=20 --receivetimeout=10000 --mqtthost 10.10.30.182 --mqttport 1883 --mqttuser ebusd --mqttjson --mqttclient=ebusd --mqttint=/etc/ebusd/mqtt-hassio.cfg"
fi
if [ "$EBUSD_OPEN" = 0 ] && [[ " $OPTS " != *" --localhost "* ]]; then
	OPTS="$OPTS --localhost"             # enkel ebus-pi praat rechtstreeks met ebusd; MQTT blijft werken
fi
if ! ls /etc/ebusd/*/ >/dev/null 2>&1 && [[ "$OPTS" == *"--configpath=/etc/ebusd"* ]]; then
	echo "LET OP: /etc/ebusd heeft geen ketelconfig. Gebruik --from-loxberry, of ebusd haalt ze online (cfg.ebusd.eu)."
	OPTS="$(echo "$OPTS" | sed 's| --configpath=/etc/ebusd||')"
fi
[ -f /etc/default/ebusd ] && cp -a /etc/default/ebusd /etc/default/ebusd.vorige
if grep -q '^EBUSD_OPTS=' /etc/default/ebusd 2>/dev/null; then
	sed -i "s|^EBUSD_OPTS=.*|EBUSD_OPTS=\"$OPTS\"|" /etc/default/ebusd
else
	echo "EBUSD_OPTS=\"$OPTS\"" >> /etc/default/ebusd
fi
echo "EBUSD_OPTS=\"$OPTS\""

# ---------------------------------------------------------------- instellingen en wachtwoorden
say "instellingen (/etc/ebuspi)"
install -d -m 770 -o root -g ebuspi /etc/ebuspi
ask_pw() {   # $1 = vraag; antwoord in $REPLY_PW (niet getoond)
	local a b
	while true; do
		read -r -s -p "$1: " a; echo
		read -r -s -p "nog eens: " b; echo
		[ "$a" = "$b" ] && break
		echo "niet gelijk, opnieuw"
	done
	REPLY_PW="$a"
}
if [ ! -f /etc/ebuspi/ebusd.env ]; then
	if [ -z "$OLD_MQTTPASS" ]; then
		ask_pw "MQTT-wachtwoord van gebruiker 'ebusd' op 10.10.30.182"
		OLD_MQTTPASS="$REPLY_PW"
	fi
	umask 077
	printf 'EBUSD_MQTTPASS=%s\n' "$OLD_MQTTPASS" > /etc/ebuspi/ebusd.env
	umask 022
	chmod 600 /etc/ebuspi/ebusd.env; chown root:root /etc/ebuspi/ebusd.env
fi
if [ ! -f /etc/ebuspi/config.json ]; then
	ask_pw "Wachtwoord voor 'ebusberry' (zelfde als in de Loxone-URL's)"
	HASH="$(printf '%s\n' "$REPLY_PW" | PYTHONPATH=/opt/ebuspi/current python3 -m ebuspi hash-password --stdin)"
	echo "Wachtwoord voor de noodhotspot 'ebus-pi' (minstens 8 tekens)"
	while true; do ask_pw "hotspot-wachtwoord"; [ ${#REPLY_PW} -ge 8 ] && break; echo "te kort"; done
	HOTSPOT_PW="$REPLY_PW"
	MQTTPASS="$(sed -n 's/^EBUSD_MQTTPASS=//p' /etc/ebuspi/ebusd.env)"
	HASH="$HASH" HOTSPOT_PW="$HOTSPOT_PW" MQTTPASS="$MQTTPASS" python3 - <<'PY'
import json, os
cfg = {
    "http": {"user": "ebusberry", "password_hash": os.environ["HASH"]},
    "mqtt": {"password": os.environ["MQTTPASS"]},
    "network": {"hotspot_password": os.environ["HOTSPOT_PW"]},
}
fd = os.open("/etc/ebuspi/config.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
PY
	unset REPLY_PW HOTSPOT_PW HASH MQTTPASS
fi
chown ebuspi:ebuspi /etc/ebuspi/config.json; chmod 600 /etc/ebuspi/config.json

# SetMode-beginwaarden van de oude ebusd (alleen lezen); ebus-pi schrijft pas als Loxone de drie temperaturen gaf
if [ -n "$SEED" ] || [ -n "$FROM" ]; then
	install -d -o ebuspi -g ebuspi /var/lib/ebuspi
	if [ -n "$SEED" ]; then SEEDARG=(--value "$SEED"); else SEEDARG=("${FROM#*@}"); fi
	sudo -u ebuspi PYTHONPATH=/opt/ebuspi/current EBUSPI_CONFIG=/etc/ebuspi/config.json \
		python3 -m ebuspi seed-setmode "${SEEDARG[@]}" || echo "(SetMode-beginwaarden niet overgenomen)"
fi

# ---------------------------------------------------------------- systeem
say "systeem: diensten, watchdog, logboek in RAM, wifi, rechten"
install -d /etc/systemd/system/ebusd.service.d /etc/systemd/journald.conf.d /etc/systemd/system.conf.d \
	/etc/NetworkManager/conf.d /etc/NetworkManager/dnsmasq-shared.d /etc/polkit-1/rules.d
install -m 644 "$SRC/system/ebusd-override.conf" /etc/systemd/system/ebusd.service.d/ebuspi.conf
install -m 644 "$SRC/system/ebuspi.service" /etc/systemd/system/ebuspi.service
install -m 644 "$SRC/system/journald.conf" /etc/systemd/journald.conf.d/ebuspi.conf
install -m 644 "$SRC/system/watchdog.conf" /etc/systemd/system.conf.d/ebuspi-watchdog.conf
install -m 644 "$SRC/system/nm-ebuspi.conf" /etc/NetworkManager/conf.d/ebuspi.conf
install -m 644 "$SRC/system/polkit-nm.rules" /etc/polkit-1/rules.d/50-ebuspi-nm.rules
install -m 644 "$SRC/system/99-ebus-adapter.rules" /etc/udev/rules.d/99-ebus-adapter.rules
install -m 440 "$SRC/system/sudoers" /etc/sudoers.d/ebuspi
visudo -cf /etc/sudoers.d/ebuspi
# ebusd-log van het pakket niet meer gebruiken (log gaat naar het journal)
rm -f /etc/logrotate.d/ebusd

# wifi: NetworkManager mag nooit opgeven met opnieuw verbinden
for c in $(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"||$2=="802-3-ethernet"{print $1}'); do
	nmcli connection modify "$c" connection.autoconnect-retries 0 802-11-wireless.powersave 2 2>/dev/null \
		|| nmcli connection modify "$c" connection.autoconnect-retries 0 || true
done

if [ "$(hostname)" != "$HOSTNAME_NEW" ]; then
	hostnamectl set-hostname "$HOSTNAME_NEW"
	sed -i "s/127.0.1.1.*/127.0.1.1\t$HOSTNAME_NEW/" /etc/hosts
fi

# wat een busgateway niet nodig heeft (minder schrijfwerk, sneller opstarten)
for s in bluetooth hciuart triggerhappy ModemManager man-db.timer e2scrub_all.timer cups cups-browsed; do
	systemctl disable --now "$s" 2>/dev/null || true
done
# enkel beveiligingsupdates van Debian automatisch; ebusd en ebus-pi niet (die via het beheer)
dpkg-reconfigure -f noninteractive unattended-upgrades || true

systemctl daemon-reload
systemctl restart systemd-journald
udevadm control --reload || true
systemctl reload NetworkManager || true
systemctl enable ebusd ebuspi
systemctl restart ebusd
systemctl restart ebuspi

say "klaar"
IP="$(hostname -I | awk '{print $1}')"
echo "Beheer:  http://${IP}/   (gebruiker ebusberry)"
echo "Loxone:  ongewijzigd, zolang dit toestel het adres van de LoxBerry krijgt"
echo "Status:  systemctl status ebusd ebuspi   ·   journalctl -u ebuspi -f"
