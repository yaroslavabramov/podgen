from __future__ import annotations

from collections.abc import Callable
from typing import Any

ToolHandler = Callable[[dict[str, Any]], Any]

__all__ = ["ToolHandler"]
