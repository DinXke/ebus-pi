"""De drie Loxone-adressen moeten zich gedragen als de LoxBerry-ebusd-plugin."""
import json
import re

from conftest import PLUGIN, http

READ = PLUGIN + "read.php?circuit=bai&update[]=Statenumber&update[]=StorageTemp&update[]=WaterPressure&update[]=FlowTemp"


def test_read_formaat_zoals_plugin(app):
    st, body, h = http(app, READ)
    assert st == 200 and h["Content-Type"] == "application/json"
    # exact wat PHP json_encode gaf: strings, geen spaties, volgorde van update[]
    assert body == '{"Statenumber":"7","StorageTemp":"34.00","WaterPressure":"1.904","FlowTemp":"26.19"}'


def test_read_loxone_herkenning(app):
    """Loxone: Check="\\iFlowTemp\\i&quot;:&quot;\\v" → zoekt FlowTemp":" en leest het getal erna."""
    _, body, _ = http(app, READ)
    for name, val in [("WaterPressure", 1.904), ("Statenumber", 7), ("StorageTemp", 34.0), ("FlowTemp", 26.19)]:
        m = re.search(re.escape(name + '":"') + r"(-?[0-9.]+)", body)
        assert m and float(m.group(1)) == val


def test_read_uit_geheugen(app, boiler):
    http(app, READ)
    n = len([c for c in boiler.commands if c.startswith("read")])
    http(app, READ)
    assert len([c for c in boiler.commands if c.startswith("read")]) == n      # tweede keer geen buslast
    assert app.store.cache_hits == 4


def test_read_vers_na_max_age(app, boiler):
    http(app, READ)
    boiler.vals["FlowTemp"] = "40.50;ok"
    app.clock.tick(31)
    _, body, _ = http(app, READ)
    assert '"FlowTemp":"40.50"' in body
    # ebusd mag zijn eigen geheugen gebruiken tot max_age
    assert any(c.startswith("read -m 30 -c bai FlowTemp") for c in boiler.commands)


def test_read_onoff_wordt_getal(app, boiler):
    _, body, _ = http(app, PLUGIN + "read.php?circuit=bai&update[]=CirPump")
    assert body == '{"CirPump":0}'


def test_read_herhaalt_bij_err_en_geeft_dan_oude_waarde(app, boiler):
    http(app, READ)
    app.clock.tick(40)
    boiler.fail_next = 100
    _, body, _ = http(app, PLUGIN + "read.php?circuit=bai&update[]=FlowTemp")
    assert body == '{"FlowTemp":"26.19"}'                                     # laatste goede (< stale_max)
    app.clock.tick(400)
    _, body, _ = http(app, PLUGIN + "read.php?circuit=bai&update[]=FlowTemp")
    assert json.loads(body)["FlowTemp"].startswith("ERR")                     # zoals de plugin


def test_read_onbekend_en_ongeldig(app):
    st, body, _ = http(app, PLUGIN + "read.php?circuit=bai&update[]=Bestaatniet")
    assert st == 200 and "ERR: element not found" in body
    st, body, _ = http(app, PLUGIN + "read.php?circuit=bai&update[]=Flow;rm%20-rf")
    assert st == 400
    st, body, _ = http(app, PLUGIN + "read.php?update[]=FlowTemp")
    assert st == 400 and "missing_query_parameters" in body


def test_auth_verplicht(app):
    assert http(app, READ, auth=None)[0] == 401
    assert http(app, READ, auth=("ebusberry", "fout"))[0] == 401
    st, _, h = http(app, READ, auth=None)
    assert "Basic" in h["WWW-Authenticate"]


def test_setmode_zoals_loxone(app, boiler):
    # Loxone stuurt elk veld apart, body '{ "flowtempdesired": <v.1> }', Content-Type maakt niet uit
    st, body, _ = http(app, PLUGIN + "setmode.php", b'{ "flowtempdesired": 45.0 }')
    assert st == 200 and json.loads(body)["flowtempdesired"] == 45.0
    assert boiler.writes == []                                                 # nog niet alle drie gekend
    http(app, PLUGIN + "setmode.php", b'{ "hwctempdesired": 50.5 }')
    http(app, PLUGIN + "setmode.php", b'{ "hwcflowtempdesired": 65.0 }')
    assert boiler.writes == [("SetMode", "auto;45;50;65;0;0;0;0;0;0")]         # (int) zoals PHP


def test_setmode_enkel_bij_wijziging_en_herhaling(app, boiler):
    for f, v in (("flowtempdesired", 45), ("hwctempdesired", 50), ("hwcflowtempdesired", 65)):
        http(app, PLUGIN + "setmode.php", json.dumps({f: v}).encode())
    assert len(boiler.writes) == 1
    for _ in range(5):                                                          # Loxone herhaalt elke 60 s
        http(app, PLUGIN + "setmode.php", b'{ "flowtempdesired": 45.0 }')
    assert len(boiler.writes) == 1
    http(app, PLUGIN + "setmode.php", b'{ "flowtempdesired": 47.0 }')
    assert boiler.writes[-1] == ("SetMode", "auto;47;50;65;0;0;0;0;0;0")
    app.clock.tick(61)
    app.setmode.flush()                                                         # vaste herhaling
    assert len(boiler.writes) == 3


def test_hex_circulatiepomp(app, boiler):
    st, body, _ = http(app, PLUGIN + "hex.php", b'{ "hex":"08B512020064" }')
    assert st == 200 and body == '"00"'
    assert boiler.hexes == ["08B512020064"] and boiler.vals["CirPump"] == "on"
    assert app.pump == 1
    st, body, _ = http(app, PLUGIN + "hex.php", b'{ "hex":"08B512020064; reboot" }')
    assert "ERR" in body and len(boiler.hexes) == 1                            # geen shell-injectie
    st, body, _ = http(app, PLUGIN + "hex.php", b"{}")
    assert st == 400 and "missing_parameters" in body


def test_read_zonder_update_geeft_lijst(app):
    _, body, _ = http(app, PLUGIN + "read.php")
    assert json.loads(body)["bai"]["FlowTemp"] == "26.19"
