"""Feed providers — one module per free public data source.

Every provider returns the same normalized shapes:

- quote dict: ``{symbol, name, last, prev_close, open, high, low,
  volume, time, source}`` — numeric fields are floats or None, absent
  symbols are simply missing from the returned mapping.
- candle dict: ``{time, open, high, low, close, volume}`` — ``time`` is
  a display string, newest last.
"""
