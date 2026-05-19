"""Coerce raw database cursor values into JSON-serialisable primitives."""
import datetime
import decimal
import uuid


def coerce_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
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
