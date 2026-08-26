"""The per-recording processing pipeline.

No re-exports; see the note in :mod:`app.scanner`. ``app.pipeline.stages`` alone reaches
the OSD engine, the detector and the plate reader, so a facade that imports it makes every
``from app.pipeline import ...`` pay for the entire inference stack.
"""
