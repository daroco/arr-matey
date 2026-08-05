"""
Shared HTTP helper, mirroring scripts/provision.py's api() function exactly (same
signature shape: base, key, method, path) so the fix actions that lift provision.py's
exact endpoint calls (README section 9's `set_field`/`get_field` pattern) don't need
translating. get_field/set_field are copied verbatim for the same reason -- these are
the dynamic {name, value} settings-field helpers every *arr dynamic form uses.
"""

import requests

DEFAULT_TIMEOUT = 30


def arr_api(base, key, method, path, **kwargs):
    r = requests.request(
        method, f"{base}{path}", headers={"X-Api-Key": key}, timeout=DEFAULT_TIMEOUT, **kwargs
    )
    r.raise_for_status()
    return r.json() if r.content else None


def set_field(fields, name, value):
    for f in fields:
        if f["name"] == name:
            f["value"] = value
            return
    fields.append({"name": name, "value": value})


def get_field(fields, name, default=None):
    return next((f.get("value", default) for f in fields if f["name"] == name), default)


class SourceError(Exception):
    """Raised by any clients/*.py function on a failed fetch. Caught by
    snapshot.py's per-source guard() -- one dead API must never take down the whole
    poll, same principle scripts/ddns-update.py already applies per-DNS-record."""

    def __init__(self, source, original):
        self.source = source
        self.original = original
        super().__init__(f"{source}: {original}")
