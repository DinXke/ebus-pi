#!/bin/bash
# ebusd installeren of bijwerken vanaf de GitHub-releases van john30/ebusd.
# De oude Debian-repo van ebusd bestaat niet meer (zie ook LoxBerry-plugin 1.1.4).
# EBUSD_VERSION=26.1 vastzetten kan; standaard de nieuwste release.
set -euo pipefail

arch=$(dpkg --print-architecture)
case $arch in
	armhf) arch=armv7 ;;
	i386) arch=386 ;;
esac
codename=$(. /etc/os-release && echo "${VERSION_CODENAME}")

api="https://api.github.com/repos/john30/ebusd/releases?per_page=10"
urls=$(curl -fsSL "$api" | grep -o "https://[^\"]*/ebusd-[^\"]*_${arch}-[a-z]*_mqtt1\.deb" || true)
if [ -n "${EBUSD_VERSION:-}" ]; then
	urls=$(echo "$urls" | grep "/ebusd-${EBUSD_VERSION}_" || true)
fi
url=$(echo "$urls" | grep "_${arch}-${codename}_mqtt1" | head -n 1 || true)
if [ -z "$url" ]; then
	# nieuwere Debian dan de laatste ebusd-build: de jongste andere build nemen
	url=$(echo "$urls" | head -n 1)
	echo "geen ebusd-build voor ${codename}, gebruik ${url##*/}"
fi
[ -n "$url" ] || { echo "geen ebusd-pakket gevonden voor ${arch}" >&2; exit 1; }

deb="/tmp/${url##*/}"
curl -fsSL -o "$deb" "$url"
echo "ebusd installeren: ${url##*/}"
DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::="--force-confdef" \
	-o Dpkg::Options::="--force-confold" install "$deb"
rm -f "$deb"
ebusd --version | head -n 1
