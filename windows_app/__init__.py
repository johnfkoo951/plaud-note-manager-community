"""Windows Community Lite localhost application.

Importing this package has no side effects and deliberately does not import
``core``.  The launcher establishes the Windows-specific privacy boundary
before any shared application module is loaded.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.3.0"
