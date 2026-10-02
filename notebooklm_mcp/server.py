"""Thin launcher. The real app + tool registration live in ``app.py`` (a module
that is never executed as ``__main__``), so ``python -m notebooklm_mcp.server``,
the ``notebooklm-mcp`` console script, and direct imports all share ONE ``mcp``
instance with the tools registered on it.

Re-exports ``mcp`` / ``main`` / ``_lifespan`` for backwards-compatible imports.
"""

from __future__ import annotations

from .app import _lifespan, main, mcp  # noqa: F401

if __name__ == "__main__":
    main()
