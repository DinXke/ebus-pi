import json
import socket
import struct

import pytest

from conftest import Clock, http, make_app
from ebuspi import phpjson
from ebuspi.checks import Checks
from ebuspi.modbus import UNKNOWN, handle_pdu
from ebuspi.netwatch import NetWatch
from ebuspi.setmode import SetMode, serialize
from ebuspi.system import System, nm_split
from ebuspi.updater import Updater, newer
from ebuspi.values import parse_status01, plugin_value
from ebuspi.web import check_password, hash_password


# ---------------------------------------------------------------- formaat
def test_phpjson():
    assert phpjson.dumps({"a": "1/2", "b": 1, "c": 45.0, "d": False, "e": "é"}) == \
        '{"a":"1\\/2","b":1,"c":45.0,"d":false,"e":"\\u00e9"}'
    assert phpjson.dumps({}) == "[]"


def test_plugin_value():
    assert plugin_value("48.50;ok") == "48.50"
    assert plugin_value("26.0;25.5;-;29.0;34.0;off") == "26.0;25.5;-;29.0;34.0;off"
    assert plugin_value("on") == 1 and plugin_value("off") == 0
    assert plugin_value("7") == "7"


def test_status01():
    s = parse_status01("26.0;25.5;-;29.0;34.0;hwc")
    assert s == {"flow": 26.0, "return": 25.5, "outdoor": None, "dhw": 29.0, "storage": 34.0, "pump": 1}


def test_serialize():
    v = {"hcmode": 0, "flowtempdesired": 45.9, "hwctempdesired": 50.0, "hwcflowtempdesired": 65.0}
    assert serialize(v) == "0;45;50;65;0;0;0;0;0;0;"                                 # GitLab-plugin
    assert serialize(v, {"0": "auto"}, trailing=False) == "auto;45;50;65;0;0;0;0;0;0"  # wat op de bus staat


def test_setmode_override_met_define(boiler, client, tmp_path):
    from ebuspi.config import DEFINE_OVERRIDE
    sm = SetMode({"message": "SetModeOverride", "define": DEFINE_OVERRIDE, "trailing_semicolon": True,
                  "hcmode_names": {}, "require": [], "repeat_s": 60, "state_file": ""}, client, clock=Clock())
    sm.update({"flowtempdesired": 40})
    assert any(c.startswith("define -r wi,bai,SetModeOverride") for c in boiler.commands)
    assert boiler.writes == [("SetModeOverride", "0;40;0;0;0;0;0;0;0;0;")]


def test_setmode_onthoudt_na_herstart(boiler, client, tmp_path):
    cfg = {"message": "SetMode", "define": "", "hcmode_names": {"0": "auto"}, "repeat_s": 60,
           "require": ["flowtempdesired", "hwctempdesired", "hwcflowtempdesired"],
           "state_file": str(tmp_path / "sm.json")}
    sm = SetMode(cfg, client, clock=Clock())
    sm.update({"flowtempdesired": 40, "hwctempdesired": 50, "hwcflowtempdesired": 60})
    sm2 = SetMode(cfg, client, clock=Clock())
    assert sm2.ready()[0] and sm2.state()["flowtempdesired"] == 40.0


def test_setmode_proefmodus(boiler, client):
    sm = SetMode({"message": "SetMode", "define": "", "require": [], "repeat_s": 60, "dry_run": True,
                  "state_file": ""}, client, clock=Clock())
    sm.update({"flowtempdesired": 40})
    assert boiler.writes == [] and sm.last_result == "proef"


def test_setmode_php_bool():
    sm = SetMode({"message": "SetMode", "define": "", "require": [], "repeat_s": 60, "dry_run": True,
                  "state_file": ""}, None, clock=Clock())
    sm.update({"disablehc": "0", "disablehwcload": 1})
    assert sm.state()["disablehc"] is False and sm.state()["disablehwcload"] is True


def test_seed_setmode(tmp_path):
    from ebuspi.app import seed_setmode
    from ebuspi.config import Config
    cfg = Config(path="", data={"setmode": {"state_file": str(tmp_path / "s.json")}})
    assert seed_setmode(cfg, value="auto;30.0;45.0;65;0;0;0;0;0;0") == 0
    d = json.loads((tmp_path / "s.json").read_text())
    assert d["values"]["flowtempdesired"] == 30.0 and d["values"]["hcmode"] == 0 and d["seen"] == []


# ---------------------------------------------------------------- wachtwoord
def test_password_hash():
    h = hash_password("x y", iterations=1000)
    assert check_password("x y", h) and not check_password("x", h) and not check_password("x", "")


def test_hash_in_config(boiler, tmp_path):
    a = make_app(boiler, tmp_path, http={"password": "", "password_hash": hash_password("pw", 1000)})
    a.start()
    a.port = a.web.server_address[1]
    try:
        assert http(a, "/api/status", auth=("ebusberry", "pw"))[0] == 200
        assert http(a, "/api/status", auth=("ebusberry", "geheim"))[0] == 401
    finally:
        a.shutdown()


# ---------------------------------------------------------------- modbus
class Bank:
    def __init__(self):
        self.h = [0] * 4
        self.i = [100, -50, UNKNOWN]

    def read_input(self, a, n):
        if a + n > len(self.i):
            raise IndexError
        return self.i[a:a + n]

    def read_holding(self, a, n):
        if a + n > len(self.h):
            raise IndexError
        return self.h[a:a + n]

    def write_holding(self, a, vals):
        if a + len(vals) > len(self.h):
            raise IndexError
        if any(v > 900 for v in vals):
            raise ValueError
        self.h[a:a + len(vals)] = vals


def test_modbus_pdu():
    b = Bank()
    assert handle_pdu(b, bytes([4, 0, 0, 0, 3])) == bytes([4, 6]) + struct.pack(">hhh", 100, -50, UNKNOWN)
    assert handle_pdu(b, bytes([6, 0, 1, 1, 0xC2])) == bytes([6, 0, 1, 1, 0xC2]) and b.h[1] == 450
    assert handle_pdu(b, bytes([16, 0, 2, 0, 2, 4, 0, 1, 0xFF, 0xFF])) == bytes([16, 0, 2, 0, 2]) and b.h[2:] == [1, -1]
    assert handle_pdu(b, bytes([3, 0, 9, 0, 1])) == bytes([0x83, 2])          # ongeldig adres
    assert handle_pdu(b, bytes([6, 0, 0, 0x03, 0x85])) == bytes([0x86, 3])    # ongeldige waarde (901)
    assert handle_pdu(b, bytes([1, 0, 0, 0, 1])) == bytes([0x81, 1])          # onbekende functie


def test_modbus_naar_setmode_en_pomp(app, boiler):
    s = socket.create_connection(("127.0.0.1", app.modbus.server_address[1]), timeout=5)

    def req(tid, pdu):
        s.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, 1) + pdu)
        head = s.recv(7)
        return s.recv(struct.unpack(">HHHB", head)[2] - 1)

    # aanvoer 45,0 / ww 50,0 / ww-aanvoer 65,0 in één keer (FC16)
    r = req(1, bytes([16, 0, 0, 0, 3, 6]) + struct.pack(">hhh", 450, 500, 650))
    assert r[0] == 16
    assert boiler.writes == [("SetMode", "auto;45;50;65;0;0;0;0;0;0")]
    req(2, bytes([6, 0, 7, 0, 1]))                                             # circulatiepomp aan
    assert boiler.hexes == ["08B512020064"]
    app.store.read_fresh("Status01")
    app.store.read_fresh("WaterPressure")
    r = req(3, bytes([4, 0, 0, 0, 7]))
    vals = struct.unpack(">7h", r[2:])
    assert vals == (260, 255, UNKNOWN, 290, 340, 0, 1904)
    r = req(4, bytes([3, 0, 0, 0, 3]))
    assert struct.unpack(">3h", r[2:]) == (450, 500, 650)
    s.close()


def test_modbus_allowlist(boiler, tmp_path):
    a = make_app(boiler, tmp_path, modbus={"allow": ["192.168.255.2"]})
    a.start()
    try:
        s = socket.create_connection(("127.0.0.1", a.modbus.server_address[1]), timeout=3)
        try:
            s.sendall(struct.pack(">HHHB", 1, 0, 6, 1) + bytes([4, 0, 0, 0, 1]))
            got = s.recv(10)
        except ConnectionError:                                                # Windows: afgebroken i.p.v. gesloten
            got = b""
        assert got == b""                                                      # verbinding gesloten, geen antwoord
    finally:
        a.shutdown()


# ---------------------------------------------------------------- controles
def test_checks(boiler, client):
    from ebuspi.values import Store
    clock = Clock()
    st = Store(client, {"retries": 1, "max_age_s": 30}, {}, clock=clock)
    sm = SetMode({"message": "SetMode", "define": "", "require": [], "repeat_s": 60, "state_file": "",
                  "hcmode_names": {"0": "auto"}}, client, clock=clock)
    info = {"signal": "acquired"}
    ch = Checks({"pressure_warn": 1.0, "pressure_alarm": 0.8}, st, sm, lambda: info, clock=clock)
    boiler.vals["WaterPressure"] = "0.75;ok"
    boiler.vals["currenterror"] = "28;-;-;-;-"
    boiler.vals["FlowTemp"] = "20.00;circuit"
    for n in ("WaterPressure", "FlowTemp"):
        st.read_fresh(n)
    new, _ = ch.step()
    codes = {f["code"]: f for f in new}
    assert codes["druk"]["level"] == "alarm" and "F.28" in codes["storing"]["text"]
    assert "sensor-FlowTemp" in codes
    # SetMode wordt genegeerd door de ketel
    sm.update({"flowtempdesired": 50})
    boiler.vals["FlowTempDesired"] = "30.00"
    boiler.vals["currenterror"] = "-;-;-;-;-"
    clock.tick(200)
    new, gone = ch.step()
    assert any(f["code"] == "setmode" for f in new) and any(f["code"] == "storing" for f in gone)
    info["signal"] = "no signal"
    assert any(f["code"] == "bus" for f in ch.evaluate())


# ---------------------------------------------------------------- watchdog
def test_watchdog_herstart_ebusd(boiler, tmp_path):
    clock = Clock()
    a = make_app(boiler, tmp_path, clock=clock, watchdog={"restart_after_s": 100, "max_restarts_per_hour": 2})
    calls = []
    a.system.restart_ebusd = lambda: calls.append(1)
    a.ebusd_info = {"signal": "no signal"}
    assert not a.watchdog_step()
    clock.tick(101)
    assert a.watchdog_step() and calls == [1]
    clock.tick(101)
    a.watchdog_step()
    clock.tick(101)
    assert not a.watchdog_step() and len(calls) == 2                            # maximum per uur
    a.ebusd_info = {"signal": "acquired"}
    assert not a.watchdog_step() and a.bad_since is None


# ---------------------------------------------------------------- netwerk
class W:
    AP_CON = "ebuspi-hotspot"

    def __init__(self):
        self.up_ = []
        self.ap = False
        self.clients = 0
        self.log = []

    def uplink(self):
        return self.up_

    def ap_active(self):
        return self.ap

    def ap_clients(self):
        return self.clients

    def saved(self):
        return [{"name": "wifi-thuis"}]

    def start_ap(self, ssid, pw, ip):
        self.ap = True
        self.log.append("start")
        return True

    def stop_ap(self):
        self.ap = False
        self.log.append("stop")

    def reconnect(self):
        self.log.append("reconnect")


class S:
    def __init__(self):
        self.captive_on = None

    def captive(self, on, ip=""):
        self.captive_on = on


def test_hotspot_na_after_s_en_weer_uit():
    clock, w, s = Clock(), W(), S()
    n = NetWatch({"after_s": 60, "retry_s": 180, "hotspot_ssid": "ebus-pi", "hotspot_ip": "10.42.0.1"}, w, s,
                 clock=clock)
    n.step()
    assert not w.ap
    clock.tick(61)
    n.step()
    assert w.ap and s.captive_on is True
    w.clients = 1
    w.up_ = [{"device": "eth0"}]
    n.step()
    assert w.ap                                                                 # iemand gebruikt hem nog
    w.clients = 0
    n.step()
    assert not w.ap and s.captive_on is False


def test_nm_split():
    assert nm_split(r"wifi\:x:802-11-wireless:yes") == ["wifi:x", "802-11-wireless", "yes"]


def test_validate_opts():
    assert System.validate_opts("-d /dev/ttyUSB0  --scanconfig") == "-d /dev/ttyUSB0 --scanconfig"
    for bad in ("--x; reboot", "--mqttpass=geheim", "", "--a `id`"):
        with pytest.raises(ValueError):
            System.validate_opts(bad)


# ---------------------------------------------------------------- updates
def test_updater_github(tmp_path):
    from ebuspi.config import Config
    import hashlib
    blob = b"pakket"
    sha = hashlib.sha256(blob).hexdigest()
    rel = {"tag_name": "v9.9.0", "assets": [{"name": "ebus-pi-9.9.0.tar.gz", "url": "A"},
                                           {"name": "ebus-pi-9.9.0.tar.gz.sha256", "url": "B"}]}

    def fetch(url, binary=False, timeout=30):
        return {"A": blob, "B": f"{sha}  x".encode()}[url] if binary else rel

    calls = []

    class Sys:
        def helper_cmd(self, *a, **k):
            calls.append(a)
            return 0, "ok", ""

    cfg = Config(path="", data={"paths": {"state_dir": str(tmp_path)}})
    u = Updater(cfg, Sys(), fetch=fetch)
    latest = u.check()
    assert latest["version"] == "9.9.0" and latest["newer"] and latest["sha256"] == sha
    assert u.install()["ok"] and calls[0][0] == "install-release"
    assert (tmp_path / "updates" / "ebus-pi-9.9.0.tar.gz").read_bytes() == blob
    # verkeerde sha256: niet installeren
    u.latest["sha256"] = "0" * 64
    assert not u.install()["ok"] and len(calls) == 1
    assert newer("0.10.0", "0.9.9") and not newer("0.1.0", "0.1.0")


def test_updater_flux_url(tmp_path):
    from ebuspi.config import Config
    cfg = Config(path="", data={"update": {"source": "url", "url": "http://flux/x.json"}})
    u = Updater(cfg, None, fetch=lambda url, **k: {"version": "0.0.1", "url": "u", "sha256": "s"})
    assert u.check()["newer"] is False


# ---------------------------------------------------------------- tunnel
def test_tunnel_voert_verzoek_lokaal_uit(app):
    import base64
    from ebuspi.tunnel import Tunnel
    t = Tunnel(app.cfg, app.port)
    app.refresh_info()                    # niet wachten op de eerste onderhoudsronde
    tok = "Basic " + base64.b64encode(b"ebusberry:geheim").decode()
    r = t.local({"method": "GET", "path": "/api/status", "headers": {"Authorization": tok}})
    assert r["status"] == 200 and json.loads(base64.b64decode(r["body_b64"]))["ebusd"] == "ok"
    r = t.local({"method": "GET", "path": "/api/status", "headers": {}})
    assert r["status"] == 401                                                   # eigen wachtwoord blijft gelden
    r = t.local({"method": "GET", "path": "/admin/plugins/ebusdplugin/api/ebusd/read.php", "headers": {}})
    assert r["status"] == 403                                                   # Loxone-adressen nooit via tunnel


# ---------------------------------------------------------------- beheer-API
def test_beheer_api(app):
    st, body, _ = http(app, "/api/status")
    d = json.loads(body)
    assert st == 200 and d["ebusd"] == "ok" and d["signal"] == "acquired" and d["adapter"] is True
    st, body, _ = http(app, "/api/config")
    assert "geheim" not in body and "********" in body
    st, body, _ = http(app, "/")
    assert st == 200 and "ebus-pi" in body
    st, body, _ = http(app, "/api/ebusd/opts", json.dumps({"opts": "--a; reboot"}).encode())
    assert st == 500 or "ongeldig" in body


# ---------------------------------------------------------------- Loxone-bewaking en ketelscan
def test_loxone_401_en_stilte(app):
    from conftest import PLUGIN as P
    from conftest import http as h
    h(app, P + "read.php?circuit=bai&update[]=FlowTemp", auth=("ebusberry", "fout"))
    h(app, P + "read.php?circuit=bai&update[]=FlowTemp", auth=("ebusberry", "fout"))
    st = app.clients.status()[0]
    assert st["ip"] == "127.0.0.1" and st["denied"] == 2 and st["user"] == "ebusberry"
    assert "fout" not in str(app.clients.status())                       # nooit het wachtwoord
    codes = {f["code"]: f for f in app.clients.findings({})}
    assert codes["loxone-401-127.0.0.1"]["level"] == "alarm"
    assert "ebusberry" in codes["loxone-401-127.0.0.1"]["text"]
    h(app, P + "read.php?circuit=bai&update[]=FlowTemp")                   # weer gelukt: melding weg
    assert not [f for f in app.clients.findings({}) if f["code"].startswith("loxone-401")]
    assert app.health()["loxone_last_ok_s"] == 0
    app.clock.tick(301)
    assert [f["code"] for f in app.clients.findings({})] == ["loxone-stil"]


def test_loxone_stil_na_start_zonder_verzoeken(boiler, tmp_path):
    from conftest import make_app
    a = make_app(boiler, tmp_path)
    assert a.clients.findings({}) == []                                    # nog binnen de aanloop
    a.clock.tick(300)
    assert [f["code"] for f in a.clients.findings({})] == ["loxone-stil"]
    assert a.clients.findings({"loxone_silence_s": 0}) == []
    assert "loxone-stil" in [f["code"] for f in a.checks.evaluate()]


def test_ketelscan_als_ebusd_de_ketel_niet_kent(boiler, tmp_path):
    from conftest import make_app
    a = make_app(boiler, tmp_path)
    boiler.unscanned = True
    a.refresh_info()
    a.store.poll_once()
    assert a.scan_step() is True and "scan 08" in boiler.commands
    assert a.scan_step() is False                                         # niet opnieuw binnen scan_retry_s
    a.store.poll_once()
    assert a.store.get("Status01")["raw"].startswith("26.0")
    a.clock.tick(700)
    assert a.scan_step() is False                                         # ketel weer gekend: geen scan meer


def test_oude_loxberry_adres_toont_beheer(app):
    from conftest import http as h
    st, body, _ = h(app, "/admin/system/index.cgi")
    assert st == 200 and "<html" in body.lower()
    assert h(app, "/admin/system/index.cgi", auth=None)[0] == 401
