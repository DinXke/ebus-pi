"""HTTP: de drie adressen van de LoxBerry-ebusd-plugin (voor Loxone) en de beheerwebsite.

Plugin-adressen (Basic auth, gebruiker 'ebusberry'):
  POST /admin/plugins/ebusdplugin/api/ebusd/setmode.php   {"flowtempdesired": 45.0} ...
  POST /admin/plugins/ebusdplugin/api/ebusd/hex.php       {"hex": "08B512020064"}
  GET  /admin/plugins/ebusdplugin/api/ebusd/read.php?circuit=bai&update[]=FlowTemp...
Beheer: / (pagina) en /api/... (JSON).
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import VERSION, phpjson
from .clients import basic_user
from .config import public

log = logging.getLogger("ebuspi.web")

PLUGIN = "/admin/plugins/ebusdplugin/api/ebusd/"
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
HEX_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}){2,40}$")
SRC_RE = re.compile(r"^[0-9A-Fa-f]{2}$")
CAPTIVE_PROBES = {"/generate_204", "/gen_204", "/hotspot-detect.html", "/library/test/success.html",
                  "/connecttest.txt", "/ncsi.txt", "/redirect", "/success.txt", "/canonical.html"}
STATIC = os.path.join(os.path.dirname(__file__), "static")


# ---------------------------------------------------------------- wachtwoorden
def hash_password(pw, iterations=200_000, salt=None):
    salt = salt or os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${dk}"


def check_password(pw, stored):
    """stored = 'pbkdf2_sha256$..' of (afgeraden) leesbaar wachtwoord. Leeg = altijd fout."""
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, it, salt, dk = stored.split("$", 3)
        except ValueError:
            return False
        cand = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(it)).hex()
        return hmac.compare_digest(cand, dk)
    return hmac.compare_digest(pw.encode(), stored.encode())


class _AuthCache:
    """PBKDF2 is traag op een Pi 3; Loxone vraagt elke 10 s. Een geslaagde header een tijd onthouden."""

    def __init__(self, ttl=600):
        self.ttl, self._ok, self._lock = ttl, {}, threading.Lock()

    def check(self, header, user, stored):
        key = hashlib.sha256(f"{header}|{user}|{stored}".encode()).hexdigest()
        now = time.time()
        with self._lock:
            if self._ok.get(key, 0) > now:
                return True
        try:
            u, _, p = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8").partition(":")
        except Exception:                      # noqa: BLE001
            return False
        ok = hmac.compare_digest(u.encode(), user.encode()) & check_password(p, stored)
        if ok:
            with self._lock:
                if len(self._ok) > 100:
                    self._ok.clear()
                self._ok[key] = now + self.ttl
        return ok


# ---------------------------------------------------------------- verzoeken
class Handler(BaseHTTPRequestHandler):
    server_version = "Apache"            # Loxone kijkt er niet naar; geen versies lekken
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):    # stil: Loxone pollt elke 10 s
        pass

    # ------------------------------------------------------------ antwoorden
    def _send(self, status, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def php(self, data, status=200):
        self._send(status, phpjson.dumps(data))

    def problem(self, typ, title, detail=None, status=400, extra=None):
        """Zoals replyError() van de plugin (application/problem+json)."""
        d = {"type": f"http://{self.headers.get('Host', '')}/api/docs/error/{typ}", "title": title}
        if detail:
            d["detail"] = detail
        d.update(extra or {})
        self._send(status, phpjson.dumps(d), "application/problem+json")

    def js(self, data, status=200):
        self._send(status, json.dumps(data, ensure_ascii=False, default=str))

    def unauthorized(self, realm):
        self._send(401, "Unauthorized", "text/plain", {"WWW-Authenticate": f'Basic realm="{realm}"'})

    # ------------------------------------------------------------ toegang
    def _auth(self, admin):
        h = self.app.cfg.section("http")
        user, pw = h["user"], h.get("password_hash") or h.get("password")
        if admin and (h.get("admin_user") or h.get("admin_password") or h.get("admin_password_hash")):
            user = h.get("admin_user") or user
            pw = h.get("admin_password_hash") or h.get("admin_password") or pw
        hdr = self.headers.get("Authorization", "")
        return hdr.startswith("Basic ") and self.server.auth.check(hdr, user, pw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            raise ValueError("te groot")
        return self.rfile.read(n) if n else b""

    def _body_obj(self):
        """JSON-body, ongeacht Content-Type (Loxone stuurt '{ "flowtempdesired": 45.0 }');
        terugval: formulier of query (zoals $_REQUEST in PHP)."""
        raw = self._body()
        try:
            d = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            if isinstance(d, dict):
                return d
        except (ValueError, UnicodeDecodeError):
            pass
        q = parse_qs(raw.decode("utf-8", "replace")) if raw else {}
        q.update(parse_qs(urlsplit(self.path).query))
        return {k: v[-1] for k, v in q.items()}

    def _captive(self, path):
        """In hotspot-modus: elke vreemde naam en elke 'ben ik online?'-test naar het beheer."""
        net = getattr(self.app, "net", None)
        if not net or not net.ap_on:
            return False
        ip = self.app.cfg.section("network")["hotspot_ip"]
        host = (self.headers.get("Host") or "").split(":")[0]
        if path in CAPTIVE_PROBES or (host and host != ip and not path.startswith(PLUGIN)):
            self._send(302, "", "text/plain", {"Location": f"http://{ip}/#wifi"})
            return True
        return False

    # ------------------------------------------------------------ routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        u = urlsplit(self.path)
        path = u.path
        try:
            if self._captive(path):
                return
            if path.startswith(PLUGIN):
                ip, user = self.client_address[0], basic_user(self.headers.get("Authorization", ""))
                if not self._auth(admin=False):
                    self.app.clients.record(ip, False, user)
                    return self.unauthorized("LoxBerry")
                self.app.clients.record(ip, True, user, path[len(PLUGIN):])
                return self._plugin(method, path[len(PLUGIN):], u.query)
            if not self._auth(admin=True):
                return self.unauthorized("ebus-pi")
            # browsers onthielden de 301 van de LoxBerry ("/" -> /admin/system/index.cgi): daar ook het beheer
            if path in ("/", "/index.html") or (path.startswith("/admin/") and path.endswith(".cgi")):
                with open(os.path.join(STATIC, "admin.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path.startswith("/api/"):
                return self._api(method, path[5:], parse_qs(u.query))
            self._send(404, "niet gevonden", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:                 # noqa: BLE001
            log.exception("http %s %s", method, path)
            try:
                self.js({"error": str(e)}, 500)
            except OSError:
                pass

    # ------------------------------------------------------------ plugin
    def _plugin(self, method, name, query):
        app = self.app
        if name == "setmode.php":
            if not app.cfg.section("setmode").get("enabled", True):
                return self.problem("setmodeoverride-disabled", "SetModeOverride is not enabled")
            if method == "POST":
                app.setmode.update(self._body_obj())
            elif method != "GET":
                return self.problem("unsupported-method", "Unsupported method")
            return self.php(app.setmode.plugin_reply())
        if name == "hex.php":
            if method != "POST":
                return self.problem("unsupported-method", "Unsupported method",
                                    f"{method} is not supported by this endpoint", 400, {"supported_methods": ["POST"]})
            d = self._body_obj()
            if "hex" not in d:
                return self.problem("missing-parameters", "Missing parameters required by endpoint", None, 400,
                                    {"missing_parameters": ["hex"]})
            return self.php(app.send_hex(str(d["hex"]), d.get("source")))
        if name == "read.php":
            if method != "GET":
                return self.problem("unsupported-method", "Unsupported method")
            q = parse_qs(query)
            upd = q.get("update[]") or q.get("update")
            if upd is not None:
                if "circuit" not in q:
                    return self.problem("missing-query-parameters", "Missing query parameters required by endpoint",
                                        None, 400, {"missing_query_parameters": ["circuit"]})
                circuit = q["circuit"][-1]
                if circuit != app.store.circuit or not all(NAME_RE.match(n) for n in upd):
                    return self.problem("query-param-invalid", "Invalid query parameter")
                return self.php({n: app.store.read_for_loxone(n) for n in upd})
            return self.php(app.find_all())
        self._send(404, "niet gevonden", "text/plain")

    # ------------------------------------------------------------ beheer-API
    def _api(self, method, name, q):
        app = self.app
        if method == "GET":
            if name == "status":
                return self.js(app.health(full=True))
            if name == "values":
                return self.js(app.store.snapshot())
            if name == "log":
                return self.js({"text": app.log_text(q.get("src", ["ebuspi"])[0], int(q.get("n", ["200"])[0]))})
            if name == "config":
                return self.js(public(app.cfg.get()))
            if name == "ebusd/opts":
                return self.js({"opts": app.system.ebusd_opts(app.cfg.section("ebusd")["defaults_file"])})
            if name == "wifi":
                return self.js({"saved": app.wifi.saved(), "net": app.net.status() if app.net else None})
            if name == "wifi/scan":
                return self.js({"networks": app.wifi.scan()})
            if name == "wifi/ip":
                return self.js(app.wifi.ip_config(q.get("name", [""])[0]))
            if name == "update":
                return self.js(app.updater.status())
            if name == "read":
                n = q.get("name", [""])[0]
                if not NAME_RE.match(n):
                    return self.js({"error": "ongeldige naam"}, 400)
                return self.js({"name": n, "result": app.store.read_fresh(n, retries=1, max_age=0)})
            return self.js({"error": "onbekend"}, 404)

        d = self._body_obj()
        if name == "ebusd/opts":
            rc, out, err = app.system.set_ebusd_opts(str(d.get("opts", "")))
            return self.js({"ok": rc == 0, "output": (out + err).strip()}, 200 if rc == 0 else 500)
        if name == "restart":
            what = d.get("what")
            fn = {"ebusd": app.system.restart_ebusd, "ebuspi": app.system.restart_self,
                  "pi": app.system.reboot}.get(what)
            if not fn:
                return self.js({"error": "onbekend"}, 400)
            # na het antwoord: anders valt de verbinding weg vóór de browser iets hoort
            threading.Timer(1.0, fn).start()
            return self.js({"ok": True})
        if name == "wifi/add":
            net = app.net
            try:
                if net:
                    net.busy.set()
                static = d.get("static") if isinstance(d.get("static"), dict) else None
                con = app.wifi.add(str(d.get("ssid", "")), str(d.get("password", "")), static)
                # verbinden in de achtergrond: op een Pi 3 valt de hotspot daarbij weg
                threading.Thread(target=app.connect_wifi, args=(con,), daemon=True).start()
                return self.js({"ok": True, "name": con})
            except (ValueError, RuntimeError) as e:
                if net:
                    net.busy.clear()
                return self.js({"error": str(e)}, 400)
        if name == "wifi/delete":
            try:
                rc, out, err = app.wifi.delete(str(d.get("name", "")))
            except ValueError as e:
                return self.js({"error": str(e)}, 400)
            return self.js({"ok": rc == 0, "output": (out + err).strip()})
        if name == "wifi/ip":
            try:
                app.wifi.set_ip(str(d.get("name", "")), d.get("static") or None)
            except (ValueError, RuntimeError) as e:
                return self.js({"error": str(e)}, 400)
            return self.js({"ok": True, "note": "actief na opnieuw verbinden of herstarten"})
        if name == "setmode":
            changes = {k: d[k] for k in ("repeat_s", "dry_run", "enabled") if k in d}
            if changes:
                app.cfg.update({"setmode": changes})
                app.setmode.cfg.update(changes)
            if d.get("write_now"):
                app.setmode.flush(reason="beheer", force=True)
            return self.js(app.setmode.status())
        if name == "update/check":
            app.updater.check()
            return self.js(app.updater.status())
        if name == "update/install":
            threading.Thread(target=app.updater.install, daemon=True).start()
            return self.js({"ok": True, "note": "bezig; de dienst herstart daarna"})
        if name == "update/settings":
            allowed = {k: d[k] for k in ("auto", "source", "repo", "url", "token", "check") if k in d}
            if allowed.get("token") == "********":
                allowed.pop("token")
            app.cfg.update({"update": allowed})
            return self.js(app.updater.status())
        if name == "tunnel/settings":
            allowed = {k: d[k] for k in ("enabled", "url", "token", "device") if k in d}
            if allowed.get("token") == "********":
                allowed.pop("token")
            app.cfg.update({"tunnel": allowed})
            return self.js(app.tunnel.status())
        if name == "ebusd/upgrade":
            rc, out, err = app.system.helper_cmd("upgrade-ebusd", timeout=600)
            return self.js({"ok": rc == 0, "output": (out + err).strip()[-3000:]})
        if name == "overlay":
            rc, out, err = app.system.overlay(bool(d.get("on")))
            return self.js({"ok": rc == 0, "output": (out + err).strip()})
        return self.js({"error": "onbekend"}, 404)


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, bind="0.0.0.0", port=80):
        self.app = app
        self.auth = _AuthCache()
        super().__init__((bind, port), Handler)

    def start(self):
        t = threading.Thread(target=self.serve_forever, name="http", daemon=True)
        t.start()
        return t


__all__ = ["WebServer", "hash_password", "check_password", "HEX_RE", "SRC_RE", "VERSION"]
