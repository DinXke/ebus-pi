"""Kleine Modbus TCP-server (slave) voor de Loxone Miniserver.

Functies: 3 (holding lezen), 4 (input lezen), 6 (één holding schrijven), 16 (meerdere holding schrijven).
De registers zelf komen uit een 'bank' met read_input(addr, n), read_holding(addr, n), write_holding(addr, vals).
Een bank gooit IndexError voor een onbekend adres en ValueError voor een ongeldige waarde.
"""
import logging
import socket
import socketserver
import struct
import threading

log = logging.getLogger("ebuspi.modbus")

ILLEGAL_FUNCTION, ILLEGAL_ADDRESS, ILLEGAL_VALUE, DEVICE_FAILURE = 1, 2, 3, 4
UNKNOWN = -32768          # 0x8000: "geen waarde"


def to_reg(v, scale=1):
    """Getal → signed 16 bit (None of buiten bereik → UNKNOWN)."""
    if v is None:
        return UNKNOWN
    try:
        r = int(round(float(v) * scale))
    except (TypeError, ValueError):
        return UNKNOWN
    return r if -32767 <= r <= 32767 else UNKNOWN


def from_reg(u16):
    return u16 - 0x10000 if u16 & 0x8000 else u16


def handle_pdu(bank, pdu):
    """Verwerkt één PDU (functiecode + data) en geeft de antwoord-PDU."""
    if not pdu:
        return bytes([0x80, ILLEGAL_FUNCTION])
    fc = pdu[0]

    def exc(code):
        return bytes([fc | 0x80, code])

    try:
        if fc in (3, 4):
            if len(pdu) != 5:
                return exc(ILLEGAL_VALUE)
            addr, n = struct.unpack(">HH", pdu[1:5])
            if not 1 <= n <= 125:
                return exc(ILLEGAL_VALUE)
            vals = (bank.read_holding if fc == 3 else bank.read_input)(addr, n)
            return bytes([fc, 2 * n]) + b"".join(struct.pack(">H", v & 0xFFFF) for v in vals)
        if fc == 6:
            if len(pdu) != 5:
                return exc(ILLEGAL_VALUE)
            addr, val = struct.unpack(">HH", pdu[1:5])
            bank.write_holding(addr, [from_reg(val)])
            return pdu[:5]
        if fc == 16:
            if len(pdu) < 6:
                return exc(ILLEGAL_VALUE)
            addr, n, nbytes = struct.unpack(">HHB", pdu[1:6])
            if not 1 <= n <= 123 or nbytes != 2 * n or len(pdu) != 6 + nbytes:
                return exc(ILLEGAL_VALUE)
            vals = [from_reg(v) for v in struct.unpack(">" + "H" * n, pdu[6:6 + nbytes])]
            bank.write_holding(addr, vals)
            return pdu[:5]
        return exc(ILLEGAL_FUNCTION)
    except IndexError:
        return exc(ILLEGAL_ADDRESS)
    except ValueError:
        return exc(ILLEGAL_VALUE)
    except Exception as e:                       # noqa: BLE001
        log.warning("modbus fc %s: %s", fc, e)
        return exc(DEVICE_FAILURE)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        srv = self.server
        peer = self.client_address[0]
        if srv.allow and peer not in srv.allow:
            log.warning("modbus: verbinding van %s geweigerd", peer)
            return
        self.request.settimeout(300)
        srv.clients += 1
        try:
            while True:
                head = _recv_exact(self.request, 7)
                tid, proto, length, unit = struct.unpack(">HHHB", head)
                if proto != 0 or not 2 <= length <= 254:
                    return
                pdu = _recv_exact(self.request, length - 1)
                if srv.unit_id not in (0, 255) and unit not in (srv.unit_id, 0, 255):
                    continue                     # niet voor ons: zoals een echte slave, geen antwoord
                resp = handle_pdu(srv.bank, pdu)
                srv.requests += 1
                self.request.sendall(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
        except (ConnectionError, socket.timeout, OSError):
            pass
        finally:
            srv.clients -= 1


class ModbusServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, bank, bind="0.0.0.0", port=502, unit_id=1, allow=None):
        self.bank, self.unit_id = bank, unit_id
        self.allow = set(allow or [])
        self.clients = 0
        self.requests = 0
        super().__init__((bind, port), _Handler)

    def start(self):
        t = threading.Thread(target=self.serve_forever, name="modbus", daemon=True)
        t.start()
        return t
