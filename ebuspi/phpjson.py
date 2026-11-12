"""JSON zoals PHP's json_encode() het schrijft (zonder spaties, '/' als '\\/', niet-ASCII als \\uXXXX).

Loxone zoekt in het antwoord van read.php naar bv. `FlowTemp":"\\v`; het formaat moet dus exact gelijk blijven.
"""
import json


def _php_float(v):
    # PHP 7.1+ (serialize_precision -1): kortste weergave, gehele floats met ".0"
    r = repr(float(v))
    return r if ("." in r or "e" in r or "n" in r) else r + ".0"


def _enc(v):
    if isinstance(v, bool) or v is None:
        return json.dumps(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _php_float(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=True).replace("/", "\\/")
    if isinstance(v, dict):
        if not v:
            return "[]"                       # lege PHP-array
        return "{" + ",".join(_enc(str(k)) + ":" + _enc(x) for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_enc(x) for x in v) + "]"
    raise TypeError(type(v))


def dumps(v):
    return _enc(v)
