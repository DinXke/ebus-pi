"""SetMode naar de ketel, zoals de LoxBerry-plugin het opbouwt.

Plugin (SetModeStorage.php): tien velden, elk als geheel getal (PHP `(int)`, dus afgekapt), gescheiden en
afgesloten met ';', geschreven met `ebusctl write -c bai SetModeOverride "<velden>"`. ebus-pi schrijft
hetzelfde bericht, maar zonder afsluitende ';' (ebusd 26.1: "invalid numeric argument"). Wat ebusd daarna als
"SetMode" toont (bv. "auto;30.0;45.0;65;0;0;0;0;0;0") is de passieve decodering van datzelfde bericht.

Verschil met de plugin: niet bij elke POST van Loxone schrijven (die herhaalt elke 60 s per veld), maar enkel
bij een wijziging en daarnaast met een vaste herhaling, want zonder SetMode valt de ketel terug.
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("ebuspi.setmode")

FIELDS = ["hcmode", "flowtempdesired", "hwctempdesired", "hwcflowtempdesired", "disablehc",
          "disablehwctapping", "disablehwcload", "remotecontrolhcpump", "releasebackup", "releasecooling"]
NUMERIC = {"hcmode": int, "flowtempdesired": float, "hwctempdesired": float, "hwcflowtempdesired": float}
DEFAULTS = {"hcmode": 0, "flowtempdesired": None, "hwctempdesired": None, "hwcflowtempdesired": None,
            "disablehc": False, "disablehwctapping": False, "disablehwcload": False,
            "remotecontrolhcpump": False, "releasebackup": False, "releasecooling": False}


def _php_int(v):
    """PHP (int): float afkappen richting nul; bool → 0/1; None → 0."""
    if v is None:
        return 0
    if isinstance(v, bool):
        return int(v)
    return int(float(v))


def _php_bool(v):
    """PHP (bool) op een JSON-waarde: 0, 0.0, "", "0", false, null zijn onwaar."""
    if isinstance(v, str):
        return v not in ("", "0")
    return bool(v)


def serialize(values, hcmode_names=None, trailing=True):
    """Plugin: elk veld (int) gevolgd door ';'. Met hcmode_names wordt hcmode de naam uit de ketelconfig."""
    parts = [str(_php_int(values.get(k))) for k in FIELDS]
    if hcmode_names:
        parts[0] = hcmode_names.get(parts[0], parts[0])
    return ";".join(parts) + (";" if trailing else "")


class SetMode:
    def __init__(self, cfg, client, clock=time.time):
        self.cfg = cfg            # dict 'setmode'
        self.client = client
        self.clock = clock
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self.values = dict(DEFAULTS)
        self.seen = set()          # velden die sinds de start van Loxone/Modbus kwamen
        self.last_written = None   # geserialiseerde tekst die laatst op de bus ging
        self.last_write_at = 0.0
        self.last_result = ""
        self.last_ok = None
        self.writes = 0
        self.skipped = 0
        self.defined = False
        self._load()

    # ------------------------------------------------------------ opslag
    def _load(self):
        path = self.cfg.get("state_file")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for k in FIELDS:
                if k in data.get("values", {}):
                    self.values[k] = data["values"][k]
            # na een herstart mogen bewaarde waarden meteen weer gebruikt worden
            self.seen = set(data.get("seen", []))
        except (OSError, ValueError):
            pass

    def _save(self):
        path = self.cfg.get("state_file")
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"values": self.values, "seen": sorted(self.seen)}, f)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("setmode bewaren faalde: %s", e)

    # ------------------------------------------------------------ invoer
    def update(self, data):
        """Velden uit een POST (JSON) of Modbus. Geeft True als er iets wijzigde. Schrijft meteen bij wijziging."""
        changed = False
        with self._lock:
            for k in FIELDS:
                if k not in data or data[k] is None:
                    continue
                v = data[k]
                try:
                    v = NUMERIC[k](float(v)) if k in NUMERIC else _php_bool(v)
                except (TypeError, ValueError):
                    continue
                if self.values.get(k) != v:
                    self.values[k] = v
                    changed = True
                self.seen.add(k)
            if changed:
                self._save()
        if changed or self.last_written is None:
            self.flush(reason="wijziging" if changed else "eerste")
        return changed

    def state(self):
        with self._lock:
            return dict(self.values)

    def ready(self):
        missing = [k for k in self.cfg.get("require", []) if k not in self.seen]
        return not missing, missing

    # ------------------------------------------------------------ schrijven
    def ensure_defined(self):
        if self.defined or not self.cfg.get("define"):
            return
        try:
            r = self.client.define(self.cfg["define"])
            self.defined = "ERR" not in r
            if not self.defined:
                log.warning("define %s: %s", self.cfg["message"], r)
        except Exception as e:                      # noqa: BLE001 - ebusd weg: volgende keer opnieuw
            log.warning("define faalde: %s", e)

    def flush(self, reason="herhaling", force=False):
        """Schrijft als er iets veranderde of de herhaaltijd voorbij is (nooit twee tegelijk)."""
        with self._wlock:
            return self._flush(reason, force)

    def _flush(self, reason, force):
        if not self.cfg.get("enabled", True):
            return None
        ok, missing = self.ready()
        if not ok:
            self.last_result = "wacht op " + ", ".join(missing)
            return None
        with self._lock:
            text = self._serialize(self.values)
        now = self.clock()
        due = now - self.last_write_at >= float(self.cfg.get("repeat_s", 60))
        if not force and text == self.last_written and not due:
            self.skipped += 1
            return None
        if self.cfg.get("dry_run"):
            log.info("SetMode (proef, niet geschreven, %s): %s", reason, text)
            self.last_written, self.last_write_at, self.last_result, self.last_ok = text, now, "proef", True
            return "proef"
        self.ensure_defined()
        try:
            r = self.client.write(self.cfg["message"], text, circuit="bai")
            if "ERR" in r and "not found" in r:     # ebusd herstart: definitie is weg
                self.defined = False
                self.ensure_defined()
                r = self.client.write(self.cfg["message"], text, circuit="bai")
        except Exception as e:                      # noqa: BLE001
            r = f"ERR: {e}"
        self.last_result, self.last_ok = r, "ERR" not in r
        self.last_write_at = now
        if self.last_ok:
            self.last_written = text
            self.writes += 1
            log.info("SetMode geschreven (%s): %s", reason, text)
        else:
            log.warning("SetMode schrijven faalde (%s): %s", reason, r)
        return r

    def _serialize(self, values):
        return serialize(values, self.cfg.get("hcmode_names") or None, bool(self.cfg.get("trailing_semicolon")))

    def status(self):
        ok, missing = self.ready()
        return {"values": self.state(), "serialized": self._serialize(self.state()), "ready": ok, "missing": missing,
                "last_written": self.last_written, "last_write_at": self.last_write_at or None,
                "last_result": self.last_result, "last_ok": self.last_ok, "writes": self.writes,
                "skipped": self.skipped, "dry_run": bool(self.cfg.get("dry_run")),
                "repeat_s": self.cfg.get("repeat_s")}

    def plugin_reply(self):
        """Antwoord zoals de plugin (get()): de bewaarde velden als JSON-object."""
        v = self.state()
        return {k: v.get(k) for k in FIELDS}
