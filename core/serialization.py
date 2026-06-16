"""Coerce raw database cursor values into JSON-serialisable primitives."""
import datetime
import decimal
import math
import uuid


def coerce_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    # Non-finite floats (NaN/Inf — e.g. AVG/STDDEV over an empty group) are not
    # JSON-compliant and crash DRF's renderer; normalise them to None.
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, decimal.Decimal):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def jsonable_rows(rows):
    """Coerce an iterable of row sequences into JSON-safe lists."""
    return [[coerce_value(cell) for cell in row] for row in rows]
