"""Wifi met terugval-hotspot (zelfde aanpak als het KHZS-timingkastje).

Elke 5 s: is er netwerk (ethernet of wifi-client)? Zo niet, na `after_s` de hotspot starten met een
captive portal (dnsmasq van NetworkManager stuurt elke naam naar het beheer). Komt het netwerk terug en
gebruikt niemand de hotspot, dan gaat hij weer uit. Zonder gebruikers wordt af en toe opnieuw geprobeerd
met de bewaarde wifi-netwerken (de Pi 3 heeft maar één radio: hotspot en client gaan niet samen).
"""
import logging
import threading
import time

log = logging.getLogger("ebuspi.net")


class NetWatch:
    def __init__(self, cfg, wifi, system, clock=time.time):
        self.cfg = cfg                  # dict 'network'
        self.wifi, self.system = wifi, system
        self.clock = clock
        self.since_uplink = clock()
        self.ap_on = False
        self.ap_reason = ""
        self.last_retry = 0.0
        self.busy = threading.Event()   # het beheer is aan het verbinden: niets forceren
        self.uplinks = []

    def start_ap(self, reason):
        c = self.cfg
        self.system.captive(True, c["hotspot_ip"])
        if self.wifi.start_ap(c["hotspot_ssid"], c.get("hotspot_password", ""), c["hotspot_ip"]):
            self.ap_on, self.ap_reason = True, reason
            log.info("hotspot %s actief (%s)", c["hotspot_ssid"], reason)
            return True
        self.system.captive(False)
        return False

    def stop_ap(self):
        self.wifi.stop_ap()
        self.system.captive(False)
        self.ap_on, self.ap_reason = False, ""
        log.info("hotspot uit")

    def step(self):
        now = self.clock()
        self.uplinks = self.wifi.uplink()
        up = bool(self.uplinks)
        if up:
            self.since_uplink = now
        self.ap_on = self.wifi.ap_active()
        if self.busy.is_set():
            return
        if not up and not self.ap_on and now - self.since_uplink >= float(self.cfg.get("after_s", 60)):
            self.start_ap("geen netwerk")
        elif self.ap_on and up and self.wifi.ap_clients() == 0:
            self.stop_ap()
        elif self.ap_on and not up and self.wifi.saved() and self.wifi.ap_clients() == 0 \
                and now - self.last_retry >= float(self.cfg.get("retry_s", 180)):
            self.last_retry = now
            log.info("hotspot zonder gebruikers: bewaarde wifi opnieuw proberen")
            self.stop_ap()
            self.wifi.reconnect()
            t0 = self.clock()
            while self.clock() - t0 < 40 and not self.wifi.uplink():
                time.sleep(3)
            if self.wifi.uplink():
                self.since_uplink = self.clock()
            else:
                self.start_ap("geen netwerk")

    def status(self):
        return {"uplink": self.uplinks, "hotspot": self.ap_on, "hotspot_reason": self.ap_reason,
                "hotspot_ssid": self.cfg.get("hotspot_ssid"), "offline_s": round(self.clock() - self.since_uplink)}

    def run(self, stop):
        while not stop.is_set():
            if self.cfg.get("enabled", True):
                try:
                    self.step()
                except Exception as e:      # noqa: BLE001 - de bewaking mag nooit stoppen
                    log.warning("netwatch: %s", e)
            stop.wait(5)
