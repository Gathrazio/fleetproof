"""Single source of the package version, importable from the lowest layer.

Lives below :mod:`fleetproof.runlog` so modules the package ``__init__``
itself imports (runlog, ledger) can stamp the version into records without a
circular import. ``fleetproof.__version__`` re-exports this value.
"""

__version__ = "0.7.0"
