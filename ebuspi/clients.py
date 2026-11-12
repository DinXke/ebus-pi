"""Wie gebruikt de Loxone-adressen? Per bron-IP: laatste geslaagde verzoek, 401's, gebruikersnaam.

Het wachtwoord wordt nooit bewaard, enkel de gebruikersnaam uit de Basic-header (ingekort).
Twee meldingen (via Checks): "Loxone krijgt 401" en "al te lang geen verzoek van Loxone".
"""
import base64
import re
import threading
import time

MAX_CLIENTS = 20
_USER_OK = re.compile(r"[^\w.@-]")


def basic_user(header):
    """Gebruikersnaam uit 'Basic …' (zonder wachtwoord), of '' als er geen (leesbare) is."""
    if not header or not header.startswith("Basic "):
        return ""
    try:
        u = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", "replace").partition(":")[0]
    except Exception:                          # noqa: BLE001
        return "?"
    return _USER_OK.sub("?", u)[:32]


class ClientWatch:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.started = clock()
        self._c = {}
        self._lock = threading.Lock()

    def record(self, ip, ok, user="", what=""):
        now = self.clock()
        with self._lock:
            c = self._c.get(ip)
            if c is None:
                if len(self._c) >= MAX_CLIENTS:
                    del self._c[min(self._c, key=lambda k: self._c[k]["last_at"])]
                c = self._c[ip] = {"ip": ip, "ok": 0, "denied": 0, "last_ok_at": None, "last_denied_at": None,
                                   "first_denied_at": None, "user": "", "denied_user": "", "last_what": ""}
            c["last_at"] = now
            c["user"] = user
            if ok:
                c["ok"] += 1
                c["last_ok_at"] = now
                c["last_what"] = what
                c["first_denied_at"] = None
            else:
                c["denied"] += 1
                c["denied_user"] = user
                c["last_denied_at"] = now
                if c["first_denied_at"] is None:
                    c["first_denied_at"] = now

    def last_ok(self):
        with self._lock:
            return max((c["last_ok_at"] for c in self._c.values() if c["last_ok_at"]), default=None)

    def status(self):
        now = self.clock()
        with self._lock:
            out = []
            for c in sorted(self._c.values(), key=lambda c: -c["last_at"]):
                d = dict(c)
                for k in ("last_ok_at", "last_denied_at", "first_denied_at", "last_at"):
                    d[k.replace("_at", "_ago_s")] = round(now - c[k]) if c[k] else None
                out.append(d)
        return out

    def findings(self, cfg):
        """Meldingen voor Checks. De tekst blijft gelijk zolang de toestand gelijk blijft (geen MQTT-ruis)."""
        now = self.clock()
        found = []
        window = float(cfg.get("loxone_denied_window_s", 600))
        with self._lock:
            clients = [dict(c) for c in self._c.values()]
        for c in clients:
            # geweigerd en sindsdien niet meer gelukt
            if c["first_denied_at"] and now - c["last_denied_at"] <= window:
                found.append({"code": f"loxone-401-{c['ip']}", "level": "alarm",
                              "text": f"Loxone ({c['ip']}) krijgt 401: gebruiker '{c['denied_user'] or '-'}'"
                                      " of wachtwoord klopt niet"})
        silence = float(cfg.get("loxone_silence_s", 300))
        if silence > 0 and now - self.started >= silence:
            last = max((c["last_ok_at"] for c in clients if c["last_ok_at"]), default=None)
            if last is None or now - last >= silence:
                found.append({"code": "loxone-stil", "level": "warn",
                              "text": f"al meer dan {int(silence // 60)} min geen geslaagd verzoek van Loxone"})
        return found
