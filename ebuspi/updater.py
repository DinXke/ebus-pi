"""Updates van ebus-pi zelf: van GitHub-releases of van een adres op Flux.

Bron 'github': GET https://api.github.com/repos/<repo>/releases/latest; asset 'ebus-pi-<versie>.tar.gz'
(en optioneel '<asset>.sha256'). Bron 'url' (bv. Flux): JSON {"version": "0.2.0", "url": "...", "sha256": "..."}.
Installeren gebeurt door de root-helper (`ebuspi-helper install-release <bestand>`): uitpakken in
/opt/ebuspi/releases/<versie>, zelftest, symlink /opt/ebuspi/current omzetten, dienst herstarten en bij een
mislukte start automatisch terug naar de vorige versie.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.request

from . import VERSION

log = logging.getLogger("ebuspi.update")
VER_RE = re.compile(r"^\d+(\.\d+){1,3}$")


def vtuple(v):
    return tuple(int(x) for x in str(v).lstrip("v").split("."))


def newer(a, b):
    """Is versie a nieuwer dan b?"""
    try:
        return vtuple(a) > vtuple(b)
    except ValueError:
        return False


class Updater:
    def __init__(self, cfg, system, fetch=None, clock=time.time):
        self.cfg = cfg                       # Config (hele object: instellingen kunnen wijzigen)
        self.system = system
        self.fetch = fetch or self._fetch
        self.clock = clock
        self.latest = None
        self.checked_at = 0.0
        self.error = ""
        self.busy = False
        self.history = []

    def _fetch(self, url, binary=False, timeout=30):
        u = self.cfg.section("update")
        headers = {"User-Agent": f"ebus-pi/{VERSION}", "Accept": "application/octet-stream" if binary
                   else "application/json"}
        if u.get("token") and "github" in url:
            headers["Authorization"] = f"Bearer {u['token']}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 - vaste, ingestelde bron
            data = r.read()
        return data if binary else json.loads(data.decode("utf-8"))

    def check(self):
        u = self.cfg.section("update")
        try:
            if u.get("source") == "url":
                d = self.fetch(u["url"])
                latest = {"version": str(d["version"]), "url": d["url"], "sha256": d.get("sha256", "")}
            else:
                d = self.fetch(f"https://api.github.com/repos/{u['repo']}/releases/latest")
                ver = str(d.get("tag_name", "")).lstrip("v")
                assets = {a["name"]: a for a in d.get("assets", [])}
                name = f"ebus-pi-{ver}.tar.gz"
                if name not in assets:
                    raise ValueError(f"release {ver} heeft geen {name}")
                sha = ""
                if name + ".sha256" in assets:
                    sha = self.fetch(assets[name + ".sha256"]["url"], binary=True).decode().split()[0]
                # API-adres van de asset: werkt ook voor een privé repo (met token)
                latest = {"version": ver, "url": assets[name]["url"], "sha256": sha}
            if not VER_RE.match(latest["version"]):
                raise ValueError(f"vreemde versie {latest['version']!r}")
            latest["newer"] = newer(latest["version"], VERSION)
            self.latest, self.error = latest, ""
        except Exception as e:              # noqa: BLE001
            self.error = str(e)
            log.warning("update-controle: %s", e)
        self.checked_at = self.clock()
        return self.latest

    def install(self, latest=None):
        latest = latest or self.latest
        if not latest or self.busy:
            return {"ok": False, "error": "niets te installeren" if not latest else "al bezig"}
        u = self.cfg.section("update")
        if u.get("require_sha256", True) and not latest.get("sha256"):
            return {"ok": False, "error": "geen sha256 bij deze release"}
        self.busy = True
        try:
            data = self.fetch(latest["url"], binary=True, timeout=120)
            if latest.get("sha256") and hashlib.sha256(data).hexdigest() != latest["sha256"].lower():
                raise ValueError("sha256 klopt niet")
            d = os.path.join(self.cfg.section("paths")["state_dir"], "updates")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"ebus-pi-{latest['version']}.tar.gz")
            with open(path, "wb") as f:
                f.write(data)
            log.info("update %s gedownload, installeren", latest["version"])
            self.history.append({"version": latest["version"], "at": self.clock()})
            # de helper herstart deze dienst: dit proces stopt daarna
            rc, out, err = self.system.helper_cmd("install-release", path, timeout=300)
            return {"ok": rc == 0, "output": (out + err).strip()[-2000:]}
        except Exception as e:              # noqa: BLE001
            self.error = str(e)
            return {"ok": False, "error": str(e)}
        finally:
            self.busy = False

    def status(self):
        u = self.cfg.section("update")
        return {"version": VERSION, "latest": self.latest, "checked_at": self.checked_at or None,
                "error": self.error, "busy": self.busy, "source": u.get("source"), "auto": u.get("auto"),
                "repo": u.get("repo"), "url": u.get("url"), "token_set": bool(u.get("token"))}

    def run(self, stop, idle=lambda: True):
        """Periodiek controleren; automatisch installeren als dat aan staat en het rustig is."""
        stop.wait(120)
        while not stop.is_set():
            u = self.cfg.section("update")
            if u.get("check", True):
                latest = self.check()
                if latest and latest.get("newer") and u.get("auto") and idle():
                    log.info("automatische update naar %s", latest["version"])
                    threading.Thread(target=self.install, args=(latest,), daemon=True).start()
            stop.wait(max(600, float(u.get("interval_h", 24)) * 3600))
