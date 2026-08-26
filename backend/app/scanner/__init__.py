"""Footage discovery and change detection.

No re-exports: every caller imports the submodule it needs, and a facade that imports all
of them only makes the cheapest import in the package as expensive as the whole of it.
"""
