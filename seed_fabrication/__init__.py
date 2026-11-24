"""A generated fabrication company: demonstration data for management boards.

Every table here is invented by ``build_warehouse`` (deterministic: the same seed
gives the same company) for the areas a fabrication shop manages but the client's
system does not hold yet: production on the shop floor, weld quality and NCRs,
purchasing and supplier delivery, dispatch, earned value, change orders and safety.
It is never mixed with a client's data: it has its own data source, flagged
``options["demo"] = True``, and every board that reads it says so.
"""
