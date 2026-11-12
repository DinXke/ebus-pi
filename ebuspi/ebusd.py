"""Client voor de TCP-poort van ebusd (8888): hetzelfde protocol als `ebusctl`.

Elke opdracht is één regel; ebusd antwoordt met één of meer regels en sluit af met een lege regel.
Eén verbinding, beschermd met een slot: ebusd verwerkt opdrachten toch na elkaar.
"""
import socket
import threading
import time


class EbusdError(Exception):
    pass


class EbusdClient:
    def __init__(self, host="127.0.0.1", port=8888, timeout=8.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock = None
        self._buf = b""
        self._lock = threading.Lock()
        self.last_ok = 0.0
        self.last_error = ""

    # ------------------------------------------------------------ verbinding
    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        self._sock, self._buf = s, b""

    def close(self):
        with self._lock:
            self._close()

    def _close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self._buf = None, b""

    def _read_reply(self):
        while b"\n\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise EbusdError("ebusd sloot de verbinding")
            self._buf += chunk
        reply, self._buf = self._buf.split(b"\n\n", 1)
        return reply.decode("utf-8", "replace").strip()

    def command(self, line):
        """Eén opdracht; geeft de tekst van het antwoord (zonder afsluitende lege regel)."""
        if "\n" in line or "\r" in line:
            raise ValueError("opdracht mag geen regeleinde bevatten")
        with self._lock:
            for attempt in (1, 2):                      # een verbinding die ebusd stil sloot: één keer opnieuw
                try:
                    if not self._sock:
                        self._connect()
                    self._sock.sendall(line.encode("utf-8") + b"\n")
                    reply = self._read_reply()
                    self.last_ok, self.last_error = time.time(), ""
                    return reply
                except (OSError, EbusdError) as e:
                    self._close()
                    self.last_error = str(e) or e.__class__.__name__
                    if attempt == 2:
                        raise EbusdError(self.last_error) from e
        raise EbusdError("onbereikbaar")

    # ------------------------------------------------------------ opdrachten
    def read(self, name, circuit="bai", max_age=None, force=False):
        args = ["read"]
        if force:
            args.append("-f")
        elif max_age is not None:
            args += ["-m", str(int(max_age))]
        args += ["-c", circuit, name]
        return self.command(" ".join(args))

    def write(self, name, value, circuit="bai"):
        return self.command(f'write -c {circuit} {name} {value}')

    def hex(self, data, source=None):
        return self.command(f"hex -s {source} {data}" if source else f"hex {data}")

    def define(self, definition, replace=True):
        return self.command(("define -r " if replace else "define ") + definition)

    def info(self):
        """`info` als woordenboek (bv. signal, reconnects, masters, version)."""
        out = {}
        for ln in self.command("info").splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                out[k.strip()] = v.strip()
        return out


def is_error(reply):
    return reply is None or "ERR" in reply
