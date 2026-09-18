"""Parse ebusd's HTTP-JSON into field descriptors + device metadata.

JSON:  { "<circuit>": { "messages": {
           "<key>": { "name":.., "write":bool, "passive":bool,
                      "fields":    { "<field>": {"name":.., "value":..} },
                      "fielddefs": [ {"name":.., "type":.., "unit":.., "values":{..}} ] } } },
         "global": {..} }
Read/write message: same `name`, write key has a "-w" suffix.
"""
from __future__ import annotations

from typing import Any, NamedTuple

# Pseudo-circuits that are not actual devices
_SKIP_CIRCUITS = {"global", "broadcast", "general", "memory", "scan"}
# Field names that make up a scan/ident message (-> device metadata instead of an entity)
_IDENT_FIELDS = {"mf", "id", "sw", "hw"}

_TYPE_BOUNDS: dict[str, tuple[float, float, float]] = {
    "UCH": (0, 254, 1), "SCH": (-127, 127, 1),
    "UIN": (0, 65534, 1), "SIN": (-32767, 32767, 1),
    "ULG": (0, 4294967294, 1), "SLG": (-2147483647, 2147483647, 1),
    "BCD": (0, 99, 1),
    "D1B": (-127, 127, 1), "D1C": (0, 100, 0.5),
    "D2B": (-128, 127, 0.1), "D2C": (-2048, 2047, 0.1),
    "EXP": (-3000, 3000, 0.1), "EXP2": (-3000, 3000, 0.1),
    "FLT": (-32.767, 32.767, 0.001), "FLR": (-32.767, 32.767, 0.001),
}


class FieldDesc(NamedTuple):
    circuit: str
    message: str
    field: str
    label: str
    unit: str | None
    values: dict[str, str] | None
    numeric: bool
    min_value: float | None
    max_value: float | None
    step: float | None
    writable: bool
    passive: bool = False  # from a passively overheard command message

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.circuit, self.message, self.field)

    @property
    def uid(self) -> str:
        return f"{self.circuit}_{self.message}_{self.field}".lower()


def _basetype(type_str: str | None) -> str:
    return (type_str or "").split(":", 1)[0].strip().upper()


def _skip_circuit(circuit: str) -> bool:
    c = circuit.lower()
    return c in _SKIP_CIRCUITS or c.startswith("scan")


def _field_names(msg: dict) -> set[str]:
    return {(fd.get("name") or "").lower() for fd in msg.get("fielddefs", [])}


def _is_ident(msg: dict) -> bool:
    names = _field_names(msg)
    return "mf" in names or _IDENT_FIELDS.issubset(names)


def _messages(cdata: Any) -> dict:
    """Return the messages dict (in the 'global' block, messages is an int!)."""
    if isinstance(cdata, dict):
        messages = cdata.get("messages")
        if isinstance(messages, dict):
            return messages
    return {}


def _iter_messages(data: dict[str, Any]):
    for circuit, cdata in data.items():
        if _skip_circuit(circuit):
            continue
        for msg in _messages(cdata).values():
            if isinstance(msg, dict):
                yield circuit, msg


def _real_fields(msg: dict) -> list[dict]:
    return [
        fd for fd in msg.get("fielddefs", [])
        if fd.get("name") and _basetype(fd.get("type")) != "IGN" and "value" not in fd
    ]


def parse_definitions(data: dict[str, Any]) -> list[FieldDesc]:
    # Only writable if the write message has EXACTLY ONE field. Multi-field
    # commands (e.g. SetMode) cannot be written field-by-field -> read-only.
    writable: set[tuple[str, str]] = set()
    passive_msgs: set[tuple[str, str]] = set()
    for circuit, msg in _iter_messages(data):
        if _is_ident(msg):
            continue
        name = msg.get("name")
        if msg.get("passive"):
            passive_msgs.add((circuit, name))
        if msg.get("write") and len(_real_fields(msg)) <= 1:
            writable.add((circuit, name))

    seen: set[tuple[str, str, str]] = set()
    out: list[FieldDesc] = []
    for circuit, msg in _iter_messages(data):
        if _is_ident(msg):
            continue
        name = msg.get("name")
        real = _real_fields(msg)
        multi = len(real) > 1
        is_passive = (circuit, name) in passive_msgs
        for fd in real:
            fname = fd["name"]
            key = (circuit, name, fname)
            if key in seen:
                continue
            seen.add(key)
            btype = _basetype(fd.get("type"))
            values = fd.get("values") or None
            lo, hi, step = _TYPE_BOUNDS.get(btype, (None, None, None))
            # Fold the divisor into the step size: a UIN/SCH with divisor 10 has
            # a real resolution of 0.1 (e.g. COP, current power, daily yields)
            # and should show 1 decimal place. Only for integer types (step >= 1);
            # fixed-point/float types already carry their resolution in the base
            # step, and a plain conversion divisor (W->kW) shouldn't force decimals
            # there. Factors (divisor < 0) make things coarser -> no decimals.
            divisor = fd.get("divisor")
            if step and step >= 1 and isinstance(divisor, (int, float)) and divisor > 1:
                lo = lo / divisor if lo is not None else None
                hi = hi / divisor if hi is not None else None
                step = step / divisor
            if fd.get("unit") == "°C":
                # realistic heating range; allows outdoor/setpoint values < 0 °C
                lo, hi, step = -60, 150, 0.5
            out.append(FieldDesc(
                circuit=circuit, message=name, field=fname,
                label=name if not multi else f"{name} {fname}",
                unit=fd.get("unit") or None, values=values,
                numeric=values is None and btype in _TYPE_BOUNDS,
                min_value=lo, max_value=hi, step=step,
                writable=(circuit, name) in writable,
                passive=is_passive,
            ))
    return out


def parse_values(data: dict[str, Any]) -> dict[tuple[str, str, str], Any]:
    values: dict[tuple[str, str, str], Any] = {}
    for circuit, msg in _iter_messages(data):
        if _is_ident(msg):
            continue
        # Pure write messages have no value; passively overheard commands
        # (u/uw, e.g. SetMode) do, though -- we want to see those.
        if msg.get("write") and not msg.get("passive"):
            continue
        name = msg.get("name")
        for fkey, fval in (msg.get("fields") or {}).items():
            if isinstance(fval, dict):
                fname = fval.get("name") or fkey
                values[(circuit, name, fname)] = fval.get("value")
    return values


def parse_decode_errors(data: dict[str, Any]) -> set[tuple[str, str]]:
    """(circuit, message) for all messages with `decodeerror` in the response.

    A response was received but doesn't match the CSV definition. Deterministic
    (occurs every cycle when the definition is wrong), so it's a good signal
    for filtering out messages -- unlike a timeout, which produces no decodeerror.
    """
    errs: set[tuple[str, str]] = set()
    for circuit, msg in _iter_messages(data):
        if _is_ident(msg):
            continue
        name = msg.get("name")
        if name and msg.get("decodeerror"):
            errs.add((circuit, name))
    return errs


def parse_ages(data: dict[str, Any]) -> dict[tuple[str, str], int]:
    """`lastup` per message (Unix time from ebusd; only present with `?verbose`).

    This lets the integration itself detect which values the bus already keeps
    fresh (other masters that ebusd passively overhears) and which are stale.
    """
    ages: dict[tuple[str, str], int] = {}
    for circuit, msg in _iter_messages(data):
        if msg.get("write") or _is_ident(msg):
            continue
        name = msg.get("name")
        lastup = msg.get("lastup")
        if name and isinstance(lastup, int):
            ages[(circuit, name)] = lastup
    return ages


def _extract_ident(fields: dict) -> dict[str, str]:
    def _val(target: str):
        for fkey, fval in fields.items():
            if isinstance(fval, dict) and (fval.get("name") or fkey).lower() == target:
                return fval.get("value")
        return None

    info: dict[str, str] = {}
    if _val("mf"):
        info["manufacturer"] = str(_val("mf"))
    if _val("id"):
        info["model"] = str(_val("id"))
    if _val("sw") is not None:
        info["sw"] = str(_val("sw"))
    if _val("hw") is not None:
        info["hw"] = str(_val("hw"))
    return info


def parse_device_meta(data: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Scan ident (MF/ID/SW/HW, under Scan.NN circuits) per address -> real circuit.

    Mapping via the target address `zz`: a scan message with zz==N is assigned
    to the real circuit whose messages have the same zz.
    """
    # 1) address -> metadata (ident messages, regardless of which circuit)
    by_addr: dict[int, dict[str, str]] = {}
    for cdata in data.values():
        for msg in _messages(cdata).values():
            if not isinstance(msg, dict) or not _is_ident(msg):
                continue
            addr = msg.get("zz")
            info = _extract_ident(msg.get("fields") or {})
            if addr is not None and info:
                by_addr.setdefault(addr, {}).update(info)

    # 2) real circuit -> address (zz of a real message)
    result: dict[str, dict[str, str]] = {}
    for circuit, cdata in data.items():
        if _skip_circuit(circuit):
            continue
        for msg in _messages(cdata).values():
            if isinstance(msg, dict) and not _is_ident(msg) and msg.get("zz") is not None:
                addr = msg["zz"]
                if addr in by_addr:
                    result[circuit] = by_addr[addr]
                break
    return result


_BOOL_ON = {"on", "yes", "true"}
_BOOL_OFF = {"off", "no", "false"}


def writable_control(desc: FieldDesc) -> bool:
    """True if a writable field is served by number/select/switch.

    Only numeric (number) or enum/binary (select/switch) fields can be
    meaningfully written. Writable text/date/hex fields don't fit any of
    these platforms -> they're shown read-only as a sensor (otherwise
    they wouldn't get an entity at all, e.g. the zone short label).
    """
    return desc.writable and (desc.numeric or bool(desc.values))


def is_error_status(desc: FieldDesc) -> bool:
    """Error-log field of the Vaillant `Currenterror` message.

    An empty error slot returns no value (None) -> the entity would otherwise
    be permanently "unavailable". Such fields should ALWAYS be visible as a
    status and show empty as "ok" (no error); show the code on a real fault.
    """
    return desc.message.lower() == "currenterror"


def is_binary(desc: FieldDesc) -> bool:
    """True if the field is a pure on/off enum (-> binary_sensor instead of sensor)."""
    if not desc.values:
        return False
    names = {str(v).lower() for v in desc.values.values()}
    return bool(names) and names <= (_BOOL_ON | _BOOL_OFF)


def value_is_on(value: object) -> bool | None:
    if value is None:
        return None
    return str(value).lower() in _BOOL_ON


def bool_tokens(desc: FieldDesc) -> tuple[str, str]:
    """(on_token, off_token) from a binary message's values map.

    Names are kept in their original form (ebusd accepts the name on write).
    """
    on_token, off_token = "on", "off"
    for name in (desc.values or {}).values():
        low = str(name).lower()
        if low in _BOOL_ON:
            on_token = name
        elif low in _BOOL_OFF:
            off_token = name
    return on_token, off_token


# Bus/adapter diagnostics from ebusd's global section.
_GLOBAL_KEYS = {
    "version", "signal", "symbolrate", "maxsymbolrate", "reconnects",
    "masters", "qq", "messages",
    "minarbitrationmicros", "maxarbitrationmicros",
    "minsymbollatency", "maxsymbollatency",
}


def parse_global(data: dict[str, Any]) -> dict[str, Any]:
    """Scalars from the `global` section (signal, rate, timing, ...)."""
    g = data.get("global")
    if not isinstance(g, dict):
        return {}
    return {k: v for k, v in g.items() if k in _GLOBAL_KEYS}
