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


def json_safe(obj):
    """Recursively coerce a response payload into strictly JSON-compliant data.

    DRF's ``JSONRenderer`` renders with ``allow_nan=False``: a NaN/Inf anywhere in
    the tree — e.g. a page-less chunk's float ``NaN`` "page" (pandas stores a
    mixed int/None column as float64), or a degenerate retrieval score — otherwise
    raises ``ValueError`` at *render* time, turning a would-be 200 into a 500
    *outside* the view's try/except. ints (incl. numpy int scalars, which DRF
    serialises via ``.tolist()``), strings, bools and None pass through untouched.
    """
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    # numpy.float64 is a subclass of float, so this catches both python and numpy
    # non-finite floats.
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    # Other numpy scalars — numpy.float32/float16, numpy.int*, numpy.bool_ — are
    # NOT python float/int subclasses, so they slip past the check above. DRF's
    # JSONRenderer (allow_nan=False) then 500s on them *at render time*, outside
    # the view's try/except. The bge embedder emits float32 similarity scores, so
    # this is a live path. Coerce every numpy scalar to a python primitive.
    if hasattr(obj, "item") and hasattr(obj, "dtype"):
        try:
            v = obj.item()
        except Exception:  # noqa: BLE001
            return str(obj)
        return (v if math.isfinite(v) else None) if isinstance(v, float) else v
    return obj
