"""Beheer via Flux: de Pi houdt zelf een uitgaande verbinding open (long-poll), er moet niets opengezet worden.

Protocol (Flux-kant implementeert dit; zie README "Beheer via Flux"):
  GET  {base}/next?device=<naam>   Authorization: Bearer <token>   (wacht max ~25 s)
       204 = niets te doen; 200 = {"id", "method", "path", "headers": {..}, "body_b64"}
  POST {base}/reply                Authorization: Bearer <token>
       {"id", "status", "headers": {..}, "body_b64"}
De Pi voert elk verzoek uit tegen zijn eigen webserver (127.0.0.1). De Authorization-header van de gebruiker
gaat mee: het beheerwachtwoord van de Pi blijft dus gelden. De Loxone-adressen gaan nooit via de tunnel.
"""
import base64
import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from . import VERSION

log = logging.getLogger("ebuspi.tunnel")
BLOCKED_PREFIX = "/admin/plugins/"
PASS_HEADERS = {"authorization", "content-type", "accept"}


class Tunnel:
    def __init__(self, cfg, local_port=80, opener=None, clock=time.time):
        self.cfg = cfg                         # Config (hele object)
        self.local_port = local_port
        self.open = opener or urllib.request.urlopen
        self.clock = clock
        self.connected = False
        self.error = ""
        self.handled = 0
        self.last_contact = 0.0

    def _req(self, url, data=None, timeout=35):
        t = self.cfg.section("tunnel")
        headers = {"Authorization": f"Bearer {t['token']}", "User-Agent": f"ebus-pi/{VERSION}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(data).encode()
        return self.open(urllib.request.Request(url, data=data, headers=headers), timeout=timeout)

    def local(self, job):
        """Verzoek uitvoeren op de eigen webserver."""
        path = str(job.get("path", "/"))
        if not path.startswith("/") or path.startswith(BLOCKED_PREFIX):
            return {"status": 403, "headers": {"Content-Type": "text/plain"},
                    "body_b64": base64.b64encode(b"niet via de tunnel").decode()}
        body = base64.b64decode(job.get("body_b64") or "")
        headers = {k: v for k, v in (job.get("headers") or {}).items() if k.lower() in PASS_HEADERS}
        conn = http.client.HTTPConnection("127.0.0.1", self.local_port, timeout=120)
        try:
            conn.request(str(job.get("method", "GET")).upper(), path, body=body or None, headers=headers)
            r = conn.getresponse()
            data = r.read()
            out_h = {k: v for k, v in r.getheaders() if k.lower() in ("content-type", "www-authenticate",
                                                                       "location", "cache-control")}
            return {"status": r.status, "headers": out_h, "body_b64": base64.b64encode(data).decode()}
        finally:
            conn.close()

    def step(self):
        t = self.cfg.section("tunnel")
        base = t["url"].rstrip("/")
        try:
            r = self._req(f"{base}/next?device={t.get('device', 'ebus-pi')}")
            self.connected, self.error, self.last_contact = True, "", self.clock()
            if r.status == 204:
                return False
            job = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            self.connected = False
            self.error = f"HTTP {e.code}"
            return None
        reply = {"id": job.get("id")}
        try:
            reply.update(self.local(job))
        except Exception as e:                  # noqa: BLE001
            reply.update({"status": 502, "headers": {"Content-Type": "text/plain"},
                          "body_b64": base64.b64encode(str(e).encode()).decode()})
        self._req(f"{base}/reply", reply, timeout=30).read()
        self.handled += 1
        return True

    def run(self, stop):
        while not stop.is_set():
            t = self.cfg.section("tunnel")
            if not t.get("enabled") or not t.get("url") or not t.get("token"):
                self.connected = False
                stop.wait(30)
                continue
            try:
                if self.step() is None:
                    stop.wait(30)
            except Exception as e:              # noqa: BLE001 - netwerk weg, Flux herstart, ...
                self.connected, self.error = False, str(e)
                stop.wait(15)

    def status(self):
        t = self.cfg.section("tunnel")
        return {"enabled": bool(t.get("enabled")), "url": t.get("url"), "connected": self.connected,
                "error": self.error, "handled": self.handled, "last_contact": self.last_contact or None}


def start(tunnel, stop):
    th = threading.Thread(target=tunnel.run, args=(stop,), name="tunnel", daemon=True)
    th.start()
    return th
