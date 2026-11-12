"""Naar buiten: gezondheidsstatus via MQTT en (optioneel) waarden meteen naar Loxone via UDP."""
import json
import logging
import socket
import threading
import time

log = logging.getLogger("ebuspi.out")


class MqttStatus:
    """Publiceert de gezondheid als retained JSON op `status_topic`; 'offline' als testament (LWT)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None
        self.connected = False
        self.error = ""
        self.published = 0

    def start(self):
        if not self.cfg.get("enabled"):
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.error = "python3-paho-mqtt ontbreekt"
            log.warning(self.error)
            return
        try:                                       # paho 2.x
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.cfg["client_id"])
        except AttributeError:                     # paho 1.x (bookworm)
            c = mqtt.Client(client_id=self.cfg["client_id"])
        if self.cfg.get("user"):
            c.username_pw_set(self.cfg["user"], self.cfg.get("password") or None)
        topic = self.cfg["status_topic"]
        c.will_set(topic, json.dumps({"state": "offline"}), qos=1, retain=True)

        def on_connect(cl, userdata, flags, rc, *a):
            self.connected = (rc == 0) if isinstance(rc, int) else not rc.is_failure
            self.error = "" if self.connected else f"verbinden: {rc}"

        def on_disconnect(cl, userdata, *a):
            self.connected = False

        c.on_connect, c.on_disconnect = on_connect, on_disconnect
        c.reconnect_delay_set(5, 120)
        try:
            c.connect_async(self.cfg["host"], int(self.cfg["port"]), keepalive=60)
            c.loop_start()
            self.client = c
        except Exception as e:                     # noqa: BLE001
            self.error = str(e)
            log.warning("mqtt: %s", e)

    def publish(self, payload):
        if self.client and self.connected:
            self.client.publish(self.cfg["status_topic"], json.dumps(payload, ensure_ascii=False), qos=1, retain=True)
            self.published += 1

    def publish_to(self, topic, payload, retain=True):
        if self.client and self.connected:
            self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=retain)

    def stop(self):
        if self.client:
            try:
                self.client.publish(self.cfg["status_topic"], json.dumps({"state": "offline"}), qos=1, retain=True)
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:                      # noqa: BLE001
                pass

    def status(self):
        return {"enabled": bool(self.cfg.get("enabled")), "connected": self.connected, "error": self.error,
                "topic": self.cfg.get("status_topic"), "published": self.published}


class LoxoneUdp:
    """Stuurt 'naam=waarde' naar een Virtual UDP Input van de Miniserver zodra een waarde wijzigt."""

    def __init__(self, cfg, clock=time.time):
        self.cfg = cfg
        self.clock = clock
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._last = {}
        self._lock = threading.Lock()
        self.sent = 0

    def notify(self, name, value):
        c = self.cfg
        if not c.get("enabled") or name not in c.get("names", []):
            return False
        now = self.clock()
        with self._lock:
            if now - self._last.get(name, 0) < float(c.get("min_interval_s", 2)):
                return False
            self._last[name] = now
        msg = f"{c.get('prefix', '')}{name}={value}".encode()
        try:
            self.sock.sendto(msg, (c["host"], int(c["port"])))
            self.sent += 1
            return True
        except OSError as e:
            log.warning("udp naar loxone: %s", e)
            return False
