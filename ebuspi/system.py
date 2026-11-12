"""Systeemkant: ebusd-dienst, logboeken, ebusd-opties, wifi (NetworkManager) en herstarten.

De dienst draait als gebruiker 'ebuspi'. Wat root nodig heeft, loopt via `sudo ebuspi-helper <opdracht>`
(vaste lijst opdrachten, zie system/ebuspi-helper). Wifi gaat rechtstreeks via nmcli (polkit-regel).
"""
import logging
import os
import shlex
import subprocess

log = logging.getLogger("ebuspi.system")


def run(cmd, timeout=30, input=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} niet gevonden"
    except subprocess.TimeoutExpired:
        return 124, "", "tijdslimiet"


class System:
    def __init__(self, helper="/usr/local/sbin/ebuspi-helper", runner=run):
        self.helper = helper
        self.run = runner

    def helper_cmd(self, *args, timeout=60, input=None):
        return self.run(["sudo", "-n", self.helper, *args], timeout=timeout, input=input)

    # ------------------------------------------------------------ diensten
    def service_state(self, name):
        rc, out, _ = self.run(["systemctl", "is-active", name], timeout=10)
        return out.strip() or "onbekend"

    def restart_ebusd(self):
        return self.helper_cmd("restart-ebusd")

    def restart_self(self):
        return self.helper_cmd("restart-ebuspi")

    def reboot(self):
        return self.helper_cmd("reboot")

    def journal(self, unit, lines=200):
        rc, out, err = self.run(["journalctl", "-u", unit, "-n", str(int(lines)), "--no-pager", "-o", "short-iso"],
                                timeout=15)
        return out if rc == 0 else err

    def tail(self, path, lines=200):
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 200 * int(lines)))
                data = f.read().decode("utf-8", "replace")
            return "\n".join(data.splitlines()[-int(lines):])
        except OSError as e:
            return f"({e})"

    # ------------------------------------------------------------ ebusd-opties
    @staticmethod
    def parse_defaults(text):
        for ln in text.splitlines():
            ln = ln.strip()
            if ln.startswith("EBUSD_OPTS="):
                v = ln[len("EBUSD_OPTS="):]
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                return v
        return ""

    def ebusd_opts(self, path="/etc/default/ebusd"):
        try:
            with open(path, encoding="utf-8") as f:
                return self.parse_defaults(f.read())
        except OSError:
            return ""

    @staticmethod
    def validate_opts(opts):
        """Enkel opties (--naam of --naam=waarde, -d pad); geen wachtwoord, geen shell-tekens."""
        if any(c in opts for c in "\n\r\"'`$;&|<>\\"):
            raise ValueError("ongeldige tekens in de opties")
        parts = shlex.split(opts)
        for p in parts:
            if "mqttpass" in p:
                raise ValueError("het MQTT-wachtwoord staat apart in /etc/ebuspi/ebusd.env")
        if not parts:
            raise ValueError("lege opties")
        return " ".join(parts)

    def set_ebusd_opts(self, opts):
        opts = self.validate_opts(opts)
        return self.helper_cmd("set-ebusd-opts", input=opts + "\n")

    # ------------------------------------------------------------ hotspot / captive portal
    def captive(self, on, ip=""):
        return self.helper_cmd("captive", "on", ip) if on else self.helper_cmd("captive", "off")

    # ------------------------------------------------------------ SD-kaart sparen
    def overlay(self, on):
        return self.helper_cmd("overlay", "on" if on else "off", timeout=180)

    def overlay_state(self):
        rc, out, _ = self.run(["findmnt", "-n", "-o", "FSTYPE", "/"], timeout=5)
        return "aan" if out.strip() == "overlay" else "uit"


# ---------------------------------------------------------------- NetworkManager
def nm_split(line):
    """nmcli -t splitst op ':' met '\\:' als escape."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


class Wifi:
    AP_CON = "ebuspi-hotspot"

    def __init__(self, runner=run, iface="wlan0"):
        self.run = runner
        self.iface = iface

    def nmcli(self, *args, timeout=30):
        return self.run(["nmcli", "-t", *args], timeout=timeout)

    def devices(self):
        rc, out, _ = self.nmcli("-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
        res = []
        for ln in out.splitlines():
            p = nm_split(ln)
            if len(p) >= 4:
                res.append({"device": p[0], "type": p[1], "state": p[2], "connection": p[3]})
        return res

    def uplink(self):
        """Verbonden via ethernet of wifi-client (niet de eigen hotspot)?"""
        return [d for d in self.devices() if d["type"] in ("ethernet", "wifi") and d["connection"] != self.AP_CON
                and d["state"].startswith("connected") and not d["state"].startswith("connected (")]

    def ap_active(self):
        return any(d["connection"] == self.AP_CON and d["state"].startswith("connected") for d in self.devices())

    def ap_clients(self):
        rc, out, _ = self.run(["iw", "dev", self.iface, "station", "dump"], timeout=10)
        return out.count("Station ")

    def scan(self):
        rc, out, _ = self.nmcli("-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "device", "wifi", "list", "ifname",
                                self.iface, "--rescan", "yes", timeout=30)
        best = {}
        for ln in out.splitlines():
            p = nm_split(ln)
            if len(p) < 5 or not p[1]:
                continue
            item = {"in_use": p[0] == "*", "ssid": p[1], "signal": int(p[2] or 0), "security": p[3],
                    "channel": p[4]}
            if p[1] not in best or item["signal"] > best[p[1]]["signal"] or item["in_use"]:
                best[p[1]] = item
        return sorted(best.values(), key=lambda x: (-x["in_use"], -x["signal"]))

    def saved(self):
        rc, out, _ = self.nmcli("-f", "NAME,TYPE,AUTOCONNECT-PRIORITY,ACTIVE", "connection", "show")
        res = []
        for ln in out.splitlines():
            p = nm_split(ln)
            if len(p) >= 4 and p[1] == "802-11-wireless" and p[0] != self.AP_CON:
                res.append({"name": p[0], "priority": p[2], "active": p[3] == "yes"})
        return res

    def ip_config(self, name):
        rc, out, _ = self.nmcli("-g", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns", "connection", "show", name)
        p = out.strip().splitlines()
        keys = ["method", "addresses", "gateway", "dns"]
        return {k: (p[i] if i < len(p) else "") for i, k in enumerate(keys)}

    @staticmethod
    def _check_text(v, what, maxlen=64):
        if not isinstance(v, str) or len(v) > maxlen or any(ord(c) < 32 for c in v):
            raise ValueError(f"ongeldige {what}")
        return v

    def add(self, ssid, password="", static=None, priority=10):
        """Nieuwe wifi bewaren en verbinden. static = {"address": "192.168.255.3/24", "gateway": .., "dns": ..}."""
        ssid = self._check_text(ssid, "SSID", 32)
        if not ssid:
            raise ValueError("SSID ontbreekt")
        password = self._check_text(password or "", "wachtwoord", 63)
        name = "wifi-" + "".join(c if c.isalnum() or c in "-_" else "_" for c in ssid)
        self.nmcli("connection", "delete", name)
        args = ["connection", "add", "type", "wifi", "ifname", self.iface, "con-name", name, "ssid", ssid,
                "connection.autoconnect", "yes", "connection.autoconnect-priority", str(int(priority)),
                "connection.autoconnect-retries", "0", "802-11-wireless.powersave", "2"]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        args += self._ip_args(static)
        rc, out, err = self.nmcli(*args)
        if rc != 0:
            raise RuntimeError(err.strip() or "bewaren mislukt")
        return name

    def _ip_args(self, static):
        if not static or not static.get("address"):
            return ["ipv4.method", "auto"]
        a = ["ipv4.method", "manual", "ipv4.addresses", self._check_text(static["address"], "adres")]
        if static.get("gateway"):
            a += ["ipv4.gateway", self._check_text(static["gateway"], "gateway")]
        if static.get("dns"):
            a += ["ipv4.dns", self._check_text(static["dns"], "dns")]
        return a

    def set_ip(self, name, static):
        rc, out, err = self.nmcli("connection", "modify", name, *self._ip_args(static))
        if rc != 0:
            raise RuntimeError(err.strip() or "wijzigen mislukt")

    def up(self, name):
        return self.nmcli("connection", "up", name, timeout=60)

    def delete(self, name):
        if name == self.AP_CON:
            raise ValueError("de hotspot kan je niet wissen")
        return self.nmcli("connection", "delete", name)

    def start_ap(self, ssid, password, ip):
        self.nmcli("connection", "delete", self.AP_CON)
        args = ["connection", "add", "type", "wifi", "ifname", self.iface, "con-name", self.AP_CON,
                "autoconnect", "no", "ssid", ssid, "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                "ipv4.method", "shared", "ipv4.addresses", f"{ip}/24", "ipv6.method", "disabled"]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password, "wifi-sec.proto", "rsn",
                     "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable"]
        rc, _, err = self.nmcli(*args)
        if rc != 0:
            log.warning("hotspot aanmaken faalde: %s", err.strip())
            return False
        rc, _, err = self.nmcli("connection", "up", self.AP_CON, timeout=40)
        if rc != 0:
            log.warning("hotspot starten faalde: %s", err.strip())
            return False
        return True

    def stop_ap(self):
        self.nmcli("connection", "down", self.AP_CON, timeout=20)

    def reconnect(self):
        return self.nmcli("device", "connect", self.iface, timeout=45)
