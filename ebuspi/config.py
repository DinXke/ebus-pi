"""Instellingen: /etc/ebuspi/config.json (rechten 600, nooit in de repo).

Ontbrekende sleutels krijgen de standaardwaarde hieronder; zo blijft een oude config werken na een update.
Wachtwoorden staan enkel in dat bestand (en het MQTT-wachtwoord van ebusd in /etc/ebuspi/ebusd.env).
"""
import copy
import json
import os
import threading

DEFAULT_PATH = os.environ.get("EBUSPI_CONFIG", "/etc/ebuspi/config.json")

# SetModeOverride zoals de LoxBerry-plugin (GitLab VVlasy/loxberry-plugin-ebusd, bin/start.sh)
DEFINE_OVERRIDE = ("wi,bai,SetModeOverride,Betriebsart,,08,B510,00,hcmode,,UCH,,,,flowtempdesired,,D1C,,,,"
                   "hwctempdesired,,D1C,,,,hwcflowtempdesired,,UCH,,,,,,IGN:1,,,,disablehc,,BI0,,,,"
                   "disablehwctapping,,BI1,,,,disablehwcload,,BI2,,,,,,IGN:1,,,,remoteControlHcPump,,BI0,,,,"
                   "releaseBackup,,BI1,,,,releaseCooling,,BI2")

DEFAULTS = {
    "http": {
        "bind": "0.0.0.0",
        "port": 80,
        # dezelfde gebruiker als de LoxBerry; het wachtwoord komt uit de lokale config
        "user": "ebusberry",
        "password": "",
        # beheerwebsite: leeg = dezelfde gebruiker/wachtwoord als de Loxone-API
        "admin_user": "",
        "admin_password": "",
    },
    "ebusd": {
        "host": "127.0.0.1",
        "port": 8888,
        "timeout_s": 8.0,
        "circuit": "bai",
        "device": "/dev/ttyUSB0",
        "logfile": "",                 # leeg: ebusd logt naar het journal (RAM)
        # opties zoals in /etc/default/ebusd (zonder MQTT-wachtwoord); enkel ter info in het beheer
        "defaults_file": "/etc/default/ebusd",
    },
    "setmode": {
        "enabled": True,
        # Zoals de LoxBerry: schrijven naar SetModeOverride uit de ketelconfig (hcmode.inc:
        # "wi,bai,SetModeOverride,..,08,B510,00"). SetMode zelf ("uw,bai,SetMode") is in ebusd 26.1 enkel passief:
        # het is de decodering van datzelfde B510 00-bericht op de bus, schrijven geeft "element not found".
        # ebusd 26.1 wil tien getallen zonder afsluitende ';' (anders "invalid numeric argument").
        # Kent de ketelconfig SetModeOverride niet: define = DEFINE_OVERRIDE (hieronder) en ebusd met --enabledefine.
        "message": "SetModeOverride",
        "define": "",
        "trailing_semicolon": False,
        # hcmode als naam vertalen (bv. {"0": "auto"}) enkel voor een bericht dat namen verwacht; leeg = als getal
        "hcmode_names": {},
        # de ketel valt terug zonder SetMode: vaste herhaling, ook als Loxone niets nieuws stuurt
        "repeat_s": 60,
        # pas schrijven als Loxone de drie temperaturen minstens één keer gaf (geen oude standaardwaarden op de bus)
        "require": ["flowtempdesired", "hwctempdesired", "hwcflowtempdesired"],
        "state_file": "/var/lib/ebuspi/setmode.json",
        # testfase naast de LoxBerry: niets schrijven, enkel loggen wat er geschreven zou worden
        "dry_run": False,
    },
    "hex": {"enabled": True, "dry_run": False},
    "read": {
        # read.php antwoordt uit het geheugen als de waarde jonger is dan dit, anders verse lezing
        "max_age_s": 30,
        # lukt een verse lezing niet, dan de laatste goede waarde tot deze leeftijd (anders de ebusd-fout, zoals de plugin)
        "stale_max_s": 300,
        "retries": 6,
    },
    "poll": {
        "enabled": True,
        "interval_s": 10,
        # Status01 = aanvoer, retour, buiten, warm water, boiler, pomp in één lezing; ebusd publiceert ze zelf op MQTT
        "messages": ["Status01", "WaterPressure", "Statenumber"],
        # waarden die Loxone via read.php vraagt, blijven vers in het geheugen zolang ze gevraagd worden
        "hot_refresh_s": 20,
        "hot_keep_s": 300,
    },
    "mqtt": {
        "enabled": True,
        "host": "10.10.30.182",
        "port": 1883,
        "user": "ebusd",
        "password": "",
        "client_id": "ebuspi",
        "status_topic": "ebusd/ebuspi/status",
        "interval_s": 60,
    },
    "modbus": {
        # Modbus TCP-server voor de Miniserver (Loxone = master); registerkaart in de README
        "enabled": True,
        "bind": "0.0.0.0",
        "port": 502,
        "unit_id": 1,
        # enkel deze adressen mogen verbinden (leeg = iedereen); Modbus heeft geen wachtwoord
        "allow": ["192.168.255.2", "127.0.0.1"],
        # circulatiepomp aan/uit zoals de Loxone-uitgang "ebusd hex"
        "pump_hex_on": "08B512020064",
        "pump_hex_off": "08B512020000",
    },
    "loxone_udp": {
        # optioneel: waarden meteen naar een Virtual UDP Input van de Miniserver sturen ("FlowTemp=48.5")
        "enabled": False,
        "host": "192.168.255.2",
        "port": 7000,
        "names": ["FlowTemp", "StorageTemp", "WaterPressure", "Statenumber"],
        "prefix": "",
        "min_interval_s": 2,
    },
    "watchdog": {
        "enabled": True,
        # ebusd onbereikbaar of zonder bussignaal: zoveel seconden wachten en dan ebusd herstarten
        "restart_after_s": 180,
        "max_restarts_per_hour": 4,
    },
    "network": {
        "enabled": True,
        "hotspot_ssid": "ebus-pi",
        "hotspot_password": "",
        "hotspot_ip": "10.42.0.1",
        "after_s": 60,
        "retry_s": 180,
    },
    "checks": {
        "enabled": True,
        "pressure_warn": 1.0,
        "pressure_alarm": 0.8,
        "pressure_high": 2.8,
        "setpoint_tolerance": 1.0,
        "slow_s": 120,
        "alarm_topic": "ebusd/ebuspi/alarm",
        "loxone_silence_s": 300,         # 0 = uit
        "loxone_denied_window_s": 600,
        "scan_address": "08",            # ketel; ebus-pi stuurt `scan 08` als ebusd hem niet kent
        "scan_retry_s": 600,
    },
    "update": {
        "check": True,
        "auto": False,
        "source": "github",            # github | url (bv. Flux)
        "repo": "DinXke/ebus-pi",
        "token": "",                   # enkel nodig voor een privé repo (leesrechten)
        "url": "",                     # bron 'url': JSON {"version","url","sha256"}
        "interval_h": 24,
        "require_sha256": True,
    },
    "tunnel": {
        # beheer via Flux: uitgaande long-poll, zie ebuspi/tunnel.py
        "enabled": False,
        "url": "",                     # bv. http://10.10.30.182:8000/api/ebuspi/tunnel
        "token": "",
        "device": "ebus-pi",
    },
    "paths": {
        "helper": "/usr/local/sbin/ebuspi-helper",
        "state_dir": "/var/lib/ebuspi",
    },
}


def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


class Config:
    """Thread-veilige houder; get() geeft altijd een kopie."""

    def __init__(self, path=DEFAULT_PATH, data=None):
        self.path = path
        self._lock = threading.Lock()
        self._data = _merge(copy.deepcopy(DEFAULTS), data if data is not None else self._read())

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def get(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def section(self, name):
        return self.get()[name]

    def update(self, changes):
        """Wijzigingen bewaren (atomair, rechten 600)."""
        with self._lock:
            _merge(self._data, changes)
            data = copy.deepcopy(self._data)
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)


def public(cfg):
    """Config zonder wachtwoorden, voor het beheer."""
    c = copy.deepcopy(cfg)
    for sec, key in (("http", "password"), ("http", "admin_password"), ("http", "password_hash"),
                     ("http", "admin_password_hash"), ("mqtt", "password"), ("network", "hotspot_password"),
                     ("update", "token"), ("tunnel", "token")):
        if c.get(sec, {}).get(key):
            c[sec][key] = "********"
    return c
