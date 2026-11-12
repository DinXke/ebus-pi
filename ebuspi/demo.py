"""Demo op een pc: het beheer bekijken zonder Pi.

  python -m ebuspi.demo                       nagebootste ketel, http://127.0.0.1:8080 (gebruiker demo/demo)
  python -m ebuspi.demo --live 192.168.255.3  echte ebusd ALLEEN LEZEN (SetMode en hex worden nooit verstuurd)
"""
import argparse
import socket
import logging
import time

from .app import App, RingLog
from .config import Config
from .ebusd import EbusdClient
from .fake_ebusd import FakeBoiler, FakeEbusd


class FakeSystem:
    helper = "(demo)"

    def __init__(self):
        self.opts = ("-d /dev/ttyUSB0 --scanconfig --configpath=/etc/ebusd --accesslevel=* --latency=20 "
                     "--receivetimeout=10000 --localhost --mqtthost 10.10.30.182 --mqttport 1883 "
                     "--mqttuser ebusd --mqttjson --mqttclient=ebusd --mqttint=/etc/ebusd/mqtt-hassio.cfg")

    def helper_cmd(self, *a, **k):
        return 0, f"(demo) {' '.join(a)}", ""

    def service_state(self, name):
        return "active"

    def restart_ebusd(self):
        return self.helper_cmd("restart-ebusd")

    restart_self = reboot = restart_ebusd

    def journal(self, unit, lines=200):
        return f"(demo) journal van {unit}"

    def tail(self, path, lines=200):
        return "(demo) 2026-11-12 10:00:00.000 [main notice] ebusd 26.1.26.1 started with auto scan"

    def ebusd_opts(self, path=None):
        return self.opts

    def set_ebusd_opts(self, opts):
        from .system import System
        self.opts = System.validate_opts(opts)
        return 0, "(demo) bewaard", ""

    def captive(self, on, ip=""):
        return 0, "", ""

    def overlay(self, on):
        return 0, "(demo)", ""

    def overlay_state(self):
        return "uit"


class FakeWifi:
    AP_CON = "ebuspi-hotspot"

    def __init__(self):
        self._saved = [{"name": "wifi-thuis", "priority": "10", "active": True}]

    def uplink(self):
        return [{"device": "wlan0", "type": "wifi", "state": "connected", "connection": "wifi-thuis"}]

    def ap_active(self):
        return False

    def ap_clients(self):
        return 0

    def saved(self):
        return self._saved

    def scan(self):
        return [{"in_use": True, "ssid": "thuis", "signal": 72, "security": "WPA2", "channel": "6"},
                {"in_use": False, "ssid": "buren", "signal": 31, "security": "WPA2", "channel": "11"}]

    def ip_config(self, name):
        return {"method": "auto", "addresses": "", "gateway": "", "dns": ""}

    def add(self, ssid, password="", static=None, priority=10):
        if not ssid:
            raise ValueError("SSID ontbreekt")
        name = "wifi-" + ssid
        self._saved.append({"name": name, "priority": str(priority), "active": False})
        return name

    def set_ip(self, name, static):
        pass

    def up(self, name):
        return 0, "", ""

    def delete(self, name):
        self._saved = [s for s in self._saved if s["name"] != name]
        return 0, "", ""


def main():
    ap = argparse.ArgumentParser(prog="ebuspi.demo")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--modbus-port", type=int, default=1502)
    ap.add_argument("--live", metavar="HOST", help="echte ebusd alleen lezend gebruiken")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if a.live:
        client = EbusdClient(a.live, 8888)
        boiler = None
    else:
        boiler = FakeBoiler(animate=True)
        srv = FakeEbusd(boiler).start()
        client = EbusdClient("127.0.0.1", srv.port)

    cfg = Config(path="", data={
        "http": {"bind": a.bind, "port": a.port, "user": "demo", "password": "demo"},
        "ebusd": {"device": "sim"},
        # live: nooit iets naar de echte ketel
        "setmode": {"state_file": "", "dry_run": bool(a.live)},
        "hex": {"dry_run": bool(a.live)},
        "modbus": {"bind": a.bind, "port": a.modbus_port, "allow": []},
        "mqtt": {"enabled": False},
        "network": {"enabled": True},
        "update": {"check": False},
        "paths": {"state_dir": "."},
    })
    app = App(cfg, client=client, system=FakeSystem(), wifi=FakeWifi())
    if a.live:
        # ook geen define of andere schrijfopdrachten
        app.setmode.cfg["define"] = ""
    app.ringlog = RingLog()
    logging.getLogger().addHandler(app.ringlog)
    app.start()
    if not a.live:
        # zoals Loxone: de drie setpoints posten
        app.setmode.update({"flowtempdesired": 45.0})
        app.setmode.update({"hwctempdesired": 50.0})
        app.setmode.update({"hwcflowtempdesired": 65.0})
    print(f"\nDemo: http://{socket.gethostname()}:{a.port}/ of http://127.0.0.1:{a.port}/  (gebruiker demo, wachtwoord demo)"
          f"{'  — LIVE alleen lezen van ' + a.live if a.live else ''}\n"
          f"Loxone-adres: http://127.0.0.1:{a.port}/admin/plugins/ebusdplugin/api/ebusd/read.php"
          f"?circuit=bai&update[]=Statenumber&update[]=StorageTemp&update[]=WaterPressure&update[]=FlowTemp\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        app.shutdown()


if __name__ == "__main__":
    main()
