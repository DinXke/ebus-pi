"""Laatste waarden van de ketel in het geheugen, plus de lezing zoals read.php van de plugin.

Plugin (read.php): per naam `ebusctl read -c <circuit> <naam>`, tot 6 keer zolang het antwoord 'ERR' bevat;
'48.50;ok' (precies twee delen) wordt '48.50'; 'on'/'off' wordt 1/0; antwoord = JSON-object in de volgorde
van update[]. Hier: eerst uit het geheugen (jonger dan max_age_s), anders een verse lezing.
"""
import logging
import threading
import time

log = logging.getLogger("ebuspi.values")


def plugin_value(raw):
    """Zelfde nabewerking als read.php."""
    result = raw
    parts = result.split(";")
    if len(parts) == 2:
        result = parts[0]
    if result in ("on", "off"):
        return 1 if result == "on" else 0
    return result


def parse_status01(raw):
    """Status01 van een Vaillant-ketel: aanvoer;retour;buiten;warm water;boiler;pomp."""
    names = ["flow", "return", "outdoor", "dhw", "storage", "pump"]
    out = {}
    for n, v in zip(names, raw.split(";")):
        v = v.strip()
        if n == "pump":
            # 0=off;1=on;2=overrun;4=hwc → draait de pomp?
            out[n] = {"off": 0, "on": 1, "overrun": 1, "hwc": 1}.get(v)
        else:
            try:
                out[n] = float(v)
            except ValueError:
                out[n] = None
    return out


class Store:
    def __init__(self, client, cfg_read, cfg_poll, circuit="bai", clock=time.time, on_change=None):
        self.client = client
        self.rcfg, self.pcfg = cfg_read, cfg_poll
        self.circuit = circuit
        self.clock = clock
        self.on_change = on_change            # callback(naam, plugin_value) bij een nieuwe waarde
        self._lock = threading.Lock()
        self._vals = {}                       # naam -> {"raw","at","err","err_at"}
        self._hot = {}                        # naam -> laatst gevraagd door Loxone
        self.reads = 0
        self.cache_hits = 0

    # ------------------------------------------------------------ geheugen
    def get(self, name):
        with self._lock:
            v = self._vals.get(name)
            return dict(v) if v else None

    def snapshot(self):
        now = self.clock()
        with self._lock:
            return {k: {"raw": v.get("raw"), "age_s": round(now - v["at"], 1) if v.get("at") else None,
                        "error": v.get("err")} for k, v in sorted(self._vals.items())}

    def _put(self, name, raw):
        now = self.clock()
        with self._lock:
            old = self._vals.get(name, {}).get("raw")
            self._vals[name] = {"raw": raw, "at": now, "err": None}
        if self.on_change and raw != old:
            try:
                self.on_change(name, plugin_value(raw))
            except Exception as e:            # noqa: BLE001 - een melder mag de lezing nooit breken
                log.warning("melden %s: %s", name, e)

    def _put_err(self, name, err):
        with self._lock:
            v = self._vals.setdefault(name, {"raw": None, "at": 0})
            v["err"], v["err_at"] = err, self.clock()

    # ------------------------------------------------------------ lezen
    def read_fresh(self, name, retries=None, max_age=None):
        """Lezing via ebusd. max_age: ebusd mag zijn eigen geheugen gebruiken als dat jonger is."""
        tries = int(retries or self.rcfg.get("retries", 6))
        result = ""
        for _ in range(max(1, tries)):
            try:
                result = self.client.read(name, circuit=self.circuit, max_age=max_age)
            except Exception as e:            # noqa: BLE001
                result = f"ERR: {e}"
            self.reads += 1
            if "ERR" not in result:
                break
        if "ERR" in result:
            self._put_err(name, result)
        else:
            self._put(name, result)
        return result

    def read_for_loxone(self, name):
        """Eén waarde voor read.php (al nabewerkt)."""
        now = self.clock()
        with self._lock:
            self._hot[name] = now
        v = self.get(name)
        if v and v.get("raw") is not None and now - v["at"] <= float(self.rcfg.get("max_age_s", 30)):
            self.cache_hits += 1
            return plugin_value(v["raw"])
        result = self.read_fresh(name, max_age=self.rcfg.get("max_age_s", 30))
        if "ERR" in result:
            v = self.get(name)
            if v and v.get("raw") is not None and now - v["at"] <= float(self.rcfg.get("stale_max_s", 300)):
                return plugin_value(v["raw"])
            return result                     # zoals de plugin: de foutmelding zelf
        return plugin_value(result)

    # ------------------------------------------------------------ achtergrond
    def hot_names(self):
        now = self.clock()
        keep = float(self.pcfg.get("hot_keep_s", 300))
        with self._lock:
            for k in [k for k, t in self._hot.items() if now - t > keep]:
                del self._hot[k]
            return list(self._hot)

    def poll_once(self):
        """Vaste berichten (Status01) en de waarden die Loxone vraagt vers houden."""
        interval = float(self.pcfg.get("interval_s", 10))
        for name in self.pcfg.get("messages", []):
            # -m iets korter dan het interval: ebusd leest echt van de bus en publiceert het zelf op MQTT
            self.read_fresh(name, retries=2, max_age=max(1, interval - 2))
        refresh = float(self.pcfg.get("hot_refresh_s", 20))
        now = self.clock()
        for name in self.hot_names():
            v = self.get(name)
            if not v or not v.get("at") or now - v["at"] >= refresh:
                self.read_fresh(name, retries=2, max_age=max(1, refresh - 2))

    def run(self, stop):
        while not stop.is_set():
            t0 = self.clock()
            if self.pcfg.get("enabled", True):
                try:
                    self.poll_once()
                except Exception as e:        # noqa: BLE001
                    log.warning("poll: %s", e)
            stop.wait(max(1.0, float(self.pcfg.get("interval_s", 10)) - (self.clock() - t0)))

    # ------------------------------------------------------------ afgeleid
    def status01(self):
        v = self.get("Status01")
        return parse_status01(v["raw"]) if v and v.get("raw") else {}

    def number(self, name):
        v = self.get(name)
        if not v or v.get("raw") is None:
            return None
        pv = plugin_value(v["raw"])
        try:
            return float(pv)
        except (TypeError, ValueError):
            return None
