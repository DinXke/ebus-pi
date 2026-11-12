"""Hoofdprogramma: alles samenbrengen en bewaken."""
import argparse
import collections
import logging
import os
import re
import socket
import threading
import time

from . import VERSION
from .checks import Checks
from .clients import ClientWatch
from .config import Config
from .ebusd import EbusdClient
from .modbus import ModbusServer, to_reg
from .netwatch import NetWatch
from .outputs import LoxoneUdp, MqttStatus
from .setmode import SetMode
from .system import System, Wifi
from .tunnel import Tunnel
from .updater import Updater
from .values import Store

log = logging.getLogger("ebuspi")
HEX_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}){2,40}$")


class RingLog(logging.Handler):
    """Laatste regels van het eigen logboek voor het beheer (het journal staat ook in RAM)."""

    def __init__(self, n=500):
        super().__init__()
        self.lines = collections.deque(maxlen=n)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        self.lines.append(self.format(record))


def sd_notify(msg):
    """systemd Type=notify zonder extra pakket."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), addr)
    except OSError:
        pass


class App:
    def __init__(self, cfg, client=None, system=None, wifi=None, clock=time.time):
        self.cfg = cfg
        c = cfg.get()
        e = c["ebusd"]
        self.clock = clock
        self.started = clock()
        self.client = client or EbusdClient(e["host"], e["port"], e["timeout_s"])
        self.system = system or System(c["paths"]["helper"])
        self.wifi = wifi or Wifi()
        self.udp = LoxoneUdp(c["loxone_udp"], clock=clock)
        self.store = Store(self.client, c["read"], c["poll"], e["circuit"], clock=clock, on_change=self.udp.notify)
        self.setmode = SetMode(c["setmode"], self.client, clock=clock)
        self.mqtt = MqttStatus(c["mqtt"])
        self.net = NetWatch(c["network"], self.wifi, self.system, clock=clock) if c["network"]["enabled"] else None
        self.clients = ClientWatch(clock=clock)
        self.checks = Checks(c["checks"], self.store, self.setmode, lambda: self.ebusd_info, clock=clock,
                             clients=self.clients)
        self.last_scan = None
        self.updater = Updater(cfg, self.system, clock=clock)
        self.tunnel = Tunnel(cfg, int(c["http"]["port"]), clock=clock)
        self.modbus = None
        self.pump = None                    # laatste stand circulatiepomp (via hex)
        self.last_hex = {}
        self.ebusd_info = {}
        self.ebusd_info_at = 0.0
        self.bad_since = None
        self.restarts = collections.deque(maxlen=20)
        self.ringlog = None
        self.stop = threading.Event()

    # ------------------------------------------------------------ acties
    def send_hex(self, data, source=None):
        """Zoals hex.php: ebusd `hex` en het (getrimde) antwoord terug."""
        hc = self.cfg.section("hex")
        if not hc.get("enabled", True):
            return "ERR: hex uitgeschakeld"
        if not HEX_RE.match(data) or (source is not None and not re.match(r"^[0-9A-Fa-f]{2}$", str(source))):
            return "ERR: invalid hex"
        mb = self.cfg.section("modbus")
        if data.upper() == mb["pump_hex_on"].upper():
            self.pump = 1
        elif data.upper() == mb["pump_hex_off"].upper():
            self.pump = 0
        if hc.get("dry_run"):
            log.info("hex (proef, niet verstuurd): %s", data)
            res = "proef"
        else:
            try:
                res = self.client.hex(data, source).strip()
            except Exception as ex:          # noqa: BLE001
                res = f"ERR: {ex}"
            log.info("hex %s -> %s", data, res)
        self.last_hex = {"hex": data, "result": res, "at": self.clock()}
        return res

    def find_all(self):
        """read.php zonder update[]: alle bekende waarden per circuit (zoals `ebusctl find -r`)."""
        try:
            out = self.client.command("find -r")
        except Exception as ex:              # noqa: BLE001
            return {"error": str(ex)}
        circuits = {}
        for line in out.splitlines():
            circuit = line.split(" ")[0]
            rest = line.replace(circuit, "", 1).strip()
            parts = rest.split("=")
            if len(parts) != 2:
                continue
            k, v = parts[0].strip(), parts[1].strip()
            if v in ("on", "off"):
                v = 1 if v == "on" else 0
            circuits.setdefault(circuit, {})[k] = v
        return circuits

    def connect_wifi(self, con):
        try:
            if self.net and self.net.ap_on:
                self.net.stop_ap()
            rc, out, err = self.wifi.up(con)
            log.info("wifi %s: %s", con, "verbonden" if rc == 0 else (err or out).strip())
        finally:
            if self.net:
                self.net.since_uplink = self.clock()
                self.net.busy.clear()

    def log_text(self, src, n=200):
        n = max(10, min(int(n), 2000))
        if src in ("ebusd", "ebusd-journal"):
            lf = self.cfg.section("ebusd").get("logfile")
            return self.system.tail(lf, n) if lf and src == "ebusd" else self.system.journal("ebusd", n)
        if src == "system":
            return self.system.journal("NetworkManager", n)
        return "\n".join(list(self.ringlog.lines)[-n:]) if self.ringlog else ""

    # ------------------------------------------------------------ gezondheid
    def refresh_info(self):
        try:
            self.ebusd_info = self.client.info()
        except Exception as ex:              # noqa: BLE001
            self.ebusd_info = {"error": str(ex)}
        self.ebusd_info_at = self.clock()

    def health(self, full=False):
        info = self.ebusd_info
        dev = self.cfg.section("ebusd")["device"]
        s01 = self.store.status01()
        h = {
            "state": "online",
            "version": VERSION,
            "uptime_s": round(self.clock() - self.started),
            "ebusd": "ok" if info and "error" not in info else "onbereikbaar",
            "signal": info.get("signal"),
            "adapter": True if dev == "sim" else (os.path.exists(dev) if dev.startswith("/dev/") else None),
            "reconnects": info.get("reconnects"),
            "setmode": {k: self.setmode.status()[k] for k in ("serialized", "last_ok", "last_write_at", "ready")},
            "flow": s01.get("flow"), "return": s01.get("return"), "dhw": s01.get("dhw"),
            "storage": s01.get("storage"), "pump": s01.get("pump"),
            "pressure": self.store.number("WaterPressure"), "statenumber": self.store.number("Statenumber"),
            "alarms": self.checks.status(),
            "watchdog_restarts": len([t for t in self.restarts if self.clock() - t < 3600]),
            "loxone_last_ok_s": (round(self.clock() - self.clients.last_ok())
                                 if self.clients.last_ok() else None),
        }
        if full:
            h.update({
                "ebusd_info": info, "ebusd_service": self.system.service_state("ebusd"),
                "setmode_full": self.setmode.status(), "values": self.store.snapshot(),
                "reads": self.store.reads, "cache_hits": self.store.cache_hits,
                "mqtt": self.mqtt.status(), "network": self.net.status() if self.net else None,
                "modbus": {"enabled": bool(self.modbus), "clients": getattr(self.modbus, "clients", 0),
                           "requests": getattr(self.modbus, "requests", 0)},
                "udp": {"enabled": self.cfg.section("loxone_udp")["enabled"], "sent": self.udp.sent},
                "update": self.updater.status(), "tunnel": self.tunnel.status(),
                "loxone": self.clients.status(), "last_scan": self.last_scan,
                "last_hex": self.last_hex, "pump_cmd": self.pump, "overlay": self.system.overlay_state(),
            })
        return h

    def watchdog_step(self):
        """ebusd onbereikbaar of zonder bussignaal te lang: ebusd herstarten (met een maximum per uur)."""
        w = self.cfg.section("watchdog")
        info = self.ebusd_info
        bad = (not info) or "error" in info or info.get("signal", "").startswith("no signal")
        now = self.clock()
        if not bad:
            self.bad_since = None
            return False
        if self.bad_since is None:
            self.bad_since = now
            return False
        recent = [t for t in self.restarts if now - t < 3600]
        if w.get("enabled", True) and now - self.bad_since >= float(w.get("restart_after_s", 180)) \
                and len(recent) < int(w.get("max_restarts_per_hour", 4)):
            log.warning("watchdog: ebusd %s sinds %ds, herstarten",
                        info.get("error") or info.get("signal") or "stil", now - self.bad_since)
            self.system.restart_ebusd()
            self.restarts.append(now)
            self.bad_since = now
            self.setmode.defined = False
            return True
        return False

    def scan_step(self):
        """Kent ebusd de ketel niet (na een start vindt de eerste scan hem soms niet): zelf `scan 08` sturen."""
        c = self.cfg.section("checks")
        addr = str(c.get("scan_address") or "")
        msgs = self.cfg.section("poll").get("messages") or []
        info = self.ebusd_info
        if not addr or not msgs or not str(info.get("signal", "")).startswith("acquired"):
            return False
        v = self.store.get(msgs[0]) or {}
        if "not found" not in str(v.get("err") or "") or (v.get("at") and v["at"] > v.get("err_at", 0)):
            return False
        now = self.clock()
        if self.last_scan and now - self.last_scan["at"] < float(c.get("scan_retry_s", 600)):
            return False
        try:
            res = self.client.command(f"scan {addr}").strip()
        except Exception as ex:              # noqa: BLE001
            res = f"ERR: {ex}"
        log.warning("ebusd kent %s niet: scan %s -> %s", msgs[0], addr, res[:200])
        self.last_scan = {"at": now, "address": addr, "result": res[:200]}
        self.setmode.defined = False
        return True

    def maintenance(self):
        """Elke 15 s: ebusd-info, watchdog, SetMode-herhaling, MQTT-status, systemd-watchdog."""
        last_mqtt = 0.0
        while not self.stop.is_set():
            try:
                self.refresh_info()
                self.watchdog_step()
                self.scan_step()
                self.setmode.flush()
                if self.cfg.section("checks").get("enabled", True):
                    new, gone = self.checks.step()
                    if new or gone:
                        self.mqtt.publish_to(self.cfg.section("checks")["alarm_topic"],
                                             {"active": self.checks.status(), "at": int(self.clock())})
                if self.clock() - last_mqtt >= float(self.cfg.section("mqtt")["interval_s"]):
                    self.mqtt.publish(self.health())
                    last_mqtt = self.clock()
            except Exception as ex:          # noqa: BLE001
                log.warning("onderhoud: %s", ex)
            sd_notify("WATCHDOG=1")
            self.stop.wait(15)

    # ------------------------------------------------------------ starten
    def start(self):
        c = self.cfg.get()
        threads = [threading.Thread(target=self.store.run, args=(self.stop,), name="poll", daemon=True),
                   threading.Thread(target=self.maintenance, name="onderhoud", daemon=True)]
        threads.append(threading.Thread(target=self.updater.run, args=(self.stop, self.idle), name="update",
                                        daemon=True))
        threads.append(threading.Thread(target=self.tunnel.run, args=(self.stop,), name="tunnel", daemon=True))
        if self.net:
            threads.append(threading.Thread(target=self.net.run, args=(self.stop,), name="net", daemon=True))
        from .web import WebServer
        self.web = WebServer(self, c["http"]["bind"], int(c["http"]["port"]))
        self.web.start()
        if c["modbus"]["enabled"]:
            try:
                self.modbus = ModbusServer(ModbusBank(self), c["modbus"]["bind"], int(c["modbus"]["port"]),
                                           int(c["modbus"]["unit_id"]), c["modbus"].get("allow"))
                self.modbus.start()
            except OSError as ex:
                log.warning("modbus: %s", ex)
        self.mqtt.start()
        for t in threads:
            t.start()
        log.info("ebus-pi %s gestart (http %s, modbus %s)", VERSION, c["http"]["port"],
                 c["modbus"]["port"] if self.modbus else "uit")
        sd_notify("READY=1")

    def idle(self):
        """Rustig genoeg voor een automatische update: geen SetMode-schrijfactie in de laatste 2 minuten."""
        return self.clock() - (self.setmode.last_write_at or 0) > 120 and not self.checks.status()

    def shutdown(self):
        self.stop.set()
        self.mqtt.stop()
        for s in (getattr(self, "web", None), self.modbus):
            if s:
                s.shutdown()
                s.server_close()


# ---------------------------------------------------------------- Modbus-registers
class ModbusBank:
    """Registerkaart: zie README (Modbus TCP). Temperaturen ×10, druk ×1000, -32768 = geen waarde."""

    HOLDING = ["flowtempdesired", "hwctempdesired", "hwcflowtempdesired", "hcmode", "disablehc",
               "disablehwctapping", "disablehwcload", "pump"]
    SCALE = {"flowtempdesired": 10, "hwctempdesired": 10, "hwcflowtempdesired": 10}

    def __init__(self, app):
        self.app = app

    def inputs(self):
        a = self.app
        s = a.store.status01()
        info = a.ebusd_info or {}
        age = None
        v = a.store.get("Status01")
        if v and v.get("at"):
            age = min(32767, int(a.clock() - v["at"]))
        return [
            to_reg(s.get("flow"), 10), to_reg(s.get("return"), 10), to_reg(s.get("outdoor"), 10),
            to_reg(s.get("dhw"), 10), to_reg(s.get("storage"), 10), to_reg(s.get("pump")),
            to_reg(a.store.number("WaterPressure"), 1000), to_reg(a.store.number("Statenumber")),
            1 if info and "error" not in info else 0,
            1 if str(info.get("signal", "")).startswith("acquired") else 0,
            to_reg(age), 1 if a.setmode.last_ok else 0,
            int(a.clock() - a.started) % 32768, to_reg(a.pump),
        ]

    def read_input(self, addr, n):
        regs = self.inputs()
        if addr + n > len(regs):
            raise IndexError
        return regs[addr:addr + n]

    def read_holding(self, addr, n):
        if addr + n > len(self.HOLDING):
            raise IndexError
        st = self.app.setmode.state()
        out = []
        for k in self.HOLDING[addr:addr + n]:
            if k == "pump":
                out.append(to_reg(self.app.pump))
            else:
                out.append(to_reg(st.get(k), self.SCALE.get(k, 1)))
        return out

    def write_holding(self, addr, vals):
        if addr + len(vals) > len(self.HOLDING):
            raise IndexError
        changes = {}
        for k, v in zip(self.HOLDING[addr:addr + len(vals)], vals):
            if k == "pump":
                if v not in (0, 1):
                    raise ValueError
                mb = self.app.cfg.section("modbus")
                self.app.send_hex(mb["pump_hex_on"] if v else mb["pump_hex_off"])
            elif k in self.SCALE:
                if not 0 <= v <= 900:
                    raise ValueError
                changes[k] = v / self.SCALE[k]
            else:
                if not 0 <= v <= 255:
                    raise ValueError
                changes[k] = v
        if changes:
            self.app.setmode.update(changes)


def main(argv=None):
    p = argparse.ArgumentParser(prog="ebuspi")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run")
    hp = sub.add_parser("hash-password", help="wachtwoord lezen van stdin, hash tonen")
    hp.add_argument("--stdin", action="store_true")
    sp = sub.add_parser("seed-setmode", help="SetMode-beginwaarden overnemen van een (oude) ebusd")
    sp.add_argument("host", nargs="?")
    sp.add_argument("--port", type=int, default=8888)
    sp.add_argument("--value", help='bv. "auto;30.0;45.0;65;0;0;0;0;0;0" (zoals read -c bai SetMode)')
    a = p.parse_args(argv)

    cfg = Config(a.config) if a.config else Config()
    if a.cmd == "hash-password":
        import getpass
        import sys
        from .web import hash_password
        pw = sys.stdin.readline().rstrip("\n") if a.stdin else getpass.getpass("Wachtwoord: ")
        print(hash_password(pw))
        return 0
    if a.cmd == "seed-setmode":
        return seed_setmode(cfg, a.host, a.port, a.value)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = App(cfg)
    app.ringlog = RingLog()
    logging.getLogger().addHandler(app.ringlog)
    app.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    return 0


def seed_setmode(cfg, host=None, port=8888, value=None):
    """Bewaart de SetMode van de oude ebusd ('read -c bai SetMode', alleen lezen) als beginstand."""
    from .setmode import FIELDS, SetMode
    c = cfg.get()["setmode"]
    raw = value if value else EbusdClient(host, port).read("SetMode")
    if "ERR" in raw:
        print(f"niet gelezen: {raw}")
        return 1
    parts = raw.split(";")
    names = {"auto": 0, "off": 1, "water": 2}           # zoals ebusd SetMode toont
    names.update({v: int(k) for k, v in (c.get("hcmode_names") or {}).items()})
    data = {}
    for k, v in zip(FIELDS, parts):
        data[k] = names.get(v, v)
    sm = SetMode(c, client=None)
    for k in FIELDS:
        if k in data and data[k] != "":
            try:
                sm.values[k] = int(data[k]) if k == "hcmode" else (float(data[k]) if k in
                    ("flowtempdesired", "hwctempdesired", "hwcflowtempdesired") else data[k] not in ("0", "off", ""))
            except ValueError:
                pass
    sm._save()
    print(f"SetMode-beginwaarden bewaard: {sm.values}")
    return 0
