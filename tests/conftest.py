import base64
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ebuspi.app import App  # noqa: E402
from ebuspi.config import Config  # noqa: E402
from ebuspi.demo import FakeSystem, FakeWifi  # noqa: E402
from ebuspi.ebusd import EbusdClient  # noqa: E402
from ebuspi.fake_ebusd import FakeBoiler, FakeEbusd  # noqa: E402

PLUGIN = "/admin/plugins/ebusdplugin/api/ebusd/"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


@pytest.fixture
def boiler():
    b = FakeBoiler()
    srv = FakeEbusd(b).start()
    b.port = srv.port
    yield b
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def client(boiler):
    c = EbusdClient("127.0.0.1", boiler.port, timeout=3)
    yield c
    c.close()


def make_app(boiler, tmp_path, clock=None, **over):
    data = {
        "http": {"bind": "127.0.0.1", "port": 0, "user": "ebusberry", "password": "geheim"},
        "ebusd": {"device": "sim"},
        "setmode": {"state_file": str(tmp_path / "setmode.json")},
        "modbus": {"bind": "127.0.0.1", "port": 0, "allow": []},
        "mqtt": {"enabled": False},
        "network": {"enabled": False},
        "update": {"check": False},
        "paths": {"state_dir": str(tmp_path)},
        "poll": {"enabled": False},
    }
    for k, v in over.items():
        data.setdefault(k, {}).update(v)
    cfg = Config(path="", data=data)
    return App(cfg, client=EbusdClient("127.0.0.1", boiler.port, timeout=3), system=FakeSystem(),
               wifi=FakeWifi(), clock=clock or Clock())


@pytest.fixture
def app(boiler, tmp_path):
    a = make_app(boiler, tmp_path)
    a.start()
    a.port = a.web.server_address[1]
    yield a
    a.shutdown()


def http(app, path, body=None, auth=("ebusberry", "geheim"), method=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=body, method=method)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)
