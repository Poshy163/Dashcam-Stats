"""Inference: object detection, tracking, plate reading and plate normalisation.

Every component degrades independently. Missing models or a missing inference runtime
disable one feature and report themselves unavailable — they never fail a recording or
stop the container from starting.

Deliberately empty of re-exports. Nothing imported the package facade -- every caller
names the submodule it wants -- while the facade itself imported all seven of them, so
``import app.ai.normalise_au`` for a pure-string helper pulled in the detector, the
tracker, the plate reader and numpy behind them. Importing a leaf module should cost the
leaf.
"""
