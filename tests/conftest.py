"""Let the real pydantic win before any suite stubs it."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import pydantic  # noqa: F401
except ImportError as exc:
    if exc.name != "pydantic":
        raise
