"""Nagebootste ebusd (TCP-protocol) met een Vaillant-ketel, voor tests en de demo.

Antwoordt zoals ebusd 26.1 op de echte LoxBerry (opgevraagd 2026-11-12, alleen lezen):
  read -c bai FlowTemp      -> 26.19;ok        read -c bai Status01 -> 26.0;25.5;-;29.0;34.0;off
  read -c bai Statenumber   -> 7               read -c bai WaterPressure -> 1.904;ok
  read -c bai SetMode       -> auto;30.0;45.0;65;0;0;0;0;0;0
"""
import math
import socketserver
import threading
import time


class FakeBoiler:
    def __init__(self, animate=False):
        self.animate = animate
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.commands = []                    # alles wat binnenkwam
        self.writes = []                      # (naam, waarde)
        self.hexes = []
        self.signal = "acquired"
        self.fail_next = 0                    # zoveel lezingen 'ERR: timeout'
        self.vals = {
            "FlowTemp": "26.19;ok", "StorageTemp": "34.00;ok", "WaterPressure": "1.904;ok", "Statenumber": "7",
            "Status01": "26.0;25.5;-;29.0;34.0;off", "FlowTempDesired": "30.00", "currenterror": "-;-;-;-;-",
            "SetMode": "auto;30.0;45.0;65;0;0;0;0;0;0", "CirPump": "off",
        }
        self.defined = set()
        self.unscanned = False                # zoals na een start waarbij de eerste scan de ketel miste

    def _tick(self):
        """Eenvoudig ketelmodel: aanvoer volgt het SetMode-setpoint, brander pendelt, boiler warmt op/koelt af."""
        if not self.animate:
            return
        now = time.time()
        dt = min(30.0, now - getattr(self, "_last", now))
        self._last = now
        st = getattr(self, "_st", None)
        if st is None:
            st = self._st = {"flow": 30.0, "ret": 27.0, "storage": 46.0, "burner": False, "pump": False,
                             "p": 1.90, "state": 0}
        sm = self.vals["SetMode"].rstrip(";").split(";")
        try:
            want_flow, want_hwc = float(sm[1]), float(sm[2])
        except (ValueError, IndexError):
            want_flow, want_hwc = 0.0, 45.0
        heat_dhw = st["storage"] < want_hwc - 5 or (st["state"] == 5 and st["storage"] < want_hwc)
        demand = want_flow >= 25 or heat_dhw
        target = 65.0 if heat_dhw else want_flow
        if demand and st["flow"] < target - 3:
            st["burner"] = True
        elif st["flow"] > target + 2 or not demand:
            st["burner"] = False
        st["pump"] = demand
        st["flow"] += dt * (0.25 if st["burner"] else -0.08) + 0.05 * math.sin(now / 7)
        st["flow"] = max(20.0, min(80.0, st["flow"]))
        st["ret"] = st["flow"] - (7.0 if st["pump"] else 1.0)
        st["storage"] += dt * (0.05 if heat_dhw and st["burner"] else -0.004)
        st["p"] = 1.90 + 0.05 * (st["flow"] - 30) / 40 + 0.005 * math.sin(now / 50)
        st["state"] = 5 if heat_dhw else (4 if st["burner"] else (1 if demand else 0))
        pump = "hwc" if heat_dhw else ("on" if st["pump"] else "off")
        out = 8 + 3 * math.sin(now / 3600)
        self.vals["FlowTemp"] = f"{st['flow']:.2f};ok"
        self.vals["StorageTemp"] = f"{st['storage']:.2f};ok"
        self.vals["Status01"] = (f"{st['flow']:.1f};{st['ret']:.1f};{out:.2f};{st['storage'] + 2:.1f};"
                                 f"{st['storage']:.1f};{pump}")
        self.vals["WaterPressure"] = f"{st['p']:.3f};ok"
        self.vals["Statenumber"] = str(st["state"])
        self.vals["Flame"] = "on" if st["burner"] else "off"

    def handle(self, line):
        with self.lock:
            self.commands.append(line)
            parts = line.split()
            if not parts:
                return "ERR: command not found"
            cmd = parts[0]
            if cmd == "info":
                return ("version: ebusd 26.1.26.1\ndevice: /dev/ttyUSB0, serial\naccess: *\n"
                        f"signal: {self.signal}\nsymbol rate: 23\nreconnects: 0\nmasters: 2\nmessages: 226\n"
                        'address 08: slave #11, scanned "MF=Vaillant;ID=BAI00;SW=0104;HW=7803", '
                        'loaded "vaillant/bai.308523.inc", "vaillant/08.bai.csv"')
            if cmd == "read":
                if self.fail_next > 0:
                    self.fail_next -= 1
                    return "ERR: read timeout"
                name = parts[-1]
                if self.unscanned:
                    return "ERR: element not found"
                self._tick()
                return self.vals.get(name, "ERR: element not found")
            if cmd == "write":
                # ebusd 26.1 + hcmode.inc: SetModeOverride is schrijfbaar (wi), SetMode enkel passief (uw)
                name, value = parts[-2], parts[-1]
                if name != "SetModeOverride" and name not in self.defined:
                    return "ERR: element not found"
                f = value.split(";")
                if len(f) != 10:
                    return "ERR: invalid position"
                try:
                    nums = [float(x) for x in f]
                except ValueError:
                    return "ERR: invalid numeric argument"
                self.writes.append((name, value))
                hc = {0: "auto", 1: "off", 2: "water"}.get(int(nums[0]), str(int(nums[0])))
                self.vals["SetMode"] = f"{hc};{nums[1]:.1f};{nums[2]:.1f};{int(nums[3])};" + ";".join(f[4:])
                self.vals["FlowTempDesired"] = f"{nums[1]:.2f}"
                return "done"
            if cmd == "hex":
                self.hexes.append(parts[-1])
                if parts[-1].upper().startswith("08B51202"):
                    self.vals["CirPump"] = "on" if parts[-1].upper().endswith("64") else "off"
                return "00"
            if cmd == "define":
                d = parts[-1].split(",")
                self.defined.add(d[2])
                return "done"
            if cmd == "scan":
                self.unscanned = False
                return "08;Vaillant;BAI00;0104;7803"
            if cmd == "find":
                return "\n".join(f"bai {k} = {v.split(';')[0]}" for k, v in sorted(self.vals.items()))
            return "ERR: command not found"


class _H(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self.wfile.write((self.server.boiler.handle(line) + "\n\n").encode())


class FakeEbusd(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, boiler=None, port=0):
        self.boiler = boiler or FakeBoiler()
        super().__init__(("127.0.0.1", port), _H)
        self.port = self.server_address[1]

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self
