"""Controles op de ketel: storing, waterdruk, sensoren, SetMode aanvaard, bus.

Ook: krijgt Loxone 401, of komt Loxone al te lang niet meer langs (clients.py).

Elke controle geeft een lijst actieve meldingen {"code", "level" (warn|alarm), "text"}.
Nieuwe of verdwenen meldingen worden gelogd en (via app) op MQTT gezet.
"""
import logging
import time

log = logging.getLogger("ebuspi.checks")

SENSOR_MSGS = ["FlowTemp", "StorageTemp", "WaterPressure"]


class Checks:
    def __init__(self, cfg, store, setmode, app_info, clock=time.time, clients=None):
        self.cfg = cfg                 # dict 'checks'
        self.store, self.setmode = store, setmode
        self.app_info = app_info       # functie → ebusd info-dict
        self.clock = clock
        self.clients = clients         # ClientWatch: Loxone-verzoeken
        self.active = {}               # code -> melding (met "since")
        self.last_slow = 0.0

    def _slow_reads(self):
        """Om de paar minuten: storing en het setpoint dat de ketel zelf gebruikt."""
        for name in ["currenterror", "FlowTempDesired"]:
            self.store.read_fresh(name, retries=2, max_age=60)

    def evaluate(self):
        c = self.cfg
        found = []
        info = self.app_info() or {}
        if "error" in info:
            found.append({"code": "ebusd", "level": "alarm", "text": f"ebusd onbereikbaar: {info['error']}"})
        elif info and not str(info.get("signal", "")).startswith("acquired"):
            found.append({"code": "bus", "level": "alarm", "text": f"geen bussignaal ({info.get('signal')})"})

        v = self.store.get("currenterror")
        if v and v.get("raw"):
            codes = [x for x in v["raw"].split(";") if x and x not in ("-", "0", "65535")]
            if codes:
                found.append({"code": "storing", "level": "alarm", "text": "ketelstoring: F." + ", F.".join(codes)})

        p = self.store.number("WaterPressure")
        if p is not None:
            if p < float(c.get("pressure_alarm", 0.8)):
                found.append({"code": "druk", "level": "alarm", "text": f"waterdruk {p:.2f} bar"})
            elif p < float(c.get("pressure_warn", 1.0)):
                found.append({"code": "druk", "level": "warn", "text": f"waterdruk laag: {p:.2f} bar"})
            elif p > float(c.get("pressure_high", 2.8)):
                found.append({"code": "druk", "level": "warn", "text": f"waterdruk hoog: {p:.2f} bar"})

        for name in SENSOR_MSGS:
            v = self.store.get(name)
            parts = (v or {}).get("raw", "") or ""
            parts = parts.split(";")
            if len(parts) == 2 and parts[1] not in ("ok", ""):
                found.append({"code": f"sensor-{name}", "level": "warn", "text": f"sensor {name}: {parts[1]}"})

        # de ketel moet het aanvoersetpoint overnemen (afgekapt zoals in SetMode); enkel als we recent schreven
        st = self.setmode.status()
        want = st["values"].get("flowtempdesired")
        have = self.store.number("FlowTempDesired")
        if st.get("last_ok") and want is not None and have is not None and not st.get("dry_run") \
                and self.clock() - (st.get("last_write_at") or 0) < 600 \
                and abs(int(want) - have) > float(c.get("setpoint_tolerance", 1.0)) and int(want) > 0:
            found.append({"code": "setmode", "level": "warn",
                          "text": f"ketel gebruikt {have:.0f} °C aanvoer, gevraagd {int(want)} °C"})

        if self.clients is not None:
            found += self.clients.findings(c)
        return found

    def step(self):
        """Geeft (nieuw, verdwenen) terug."""
        now = self.clock()
        if now - self.last_slow >= float(self.cfg.get("slow_s", 120)):
            self.last_slow = now
            self._slow_reads()
        found = {f["code"]: f for f in self.evaluate()}
        new = [f for k, f in found.items() if k not in self.active or self.active[k]["text"] != f["text"]]
        gone = [f for k, f in self.active.items() if k not in found]
        for f in new:
            f["since"] = self.active.get(f["code"], {}).get("since", now)
            log.warning("melding: %s", f["text"])
        for f in gone:
            log.info("opgelost: %s", f["text"])
        self.active = {k: dict(f, since=self.active.get(k, f).get("since", now)) for k, f in found.items()}
        return new, gone

    def status(self):
        return sorted(self.active.values(), key=lambda f: (f["level"] != "alarm", f["code"]))
