"""MVA Canvas: a small Python API over the Minerva Virtual Academy Canvas instance."""
from .api import MvaCanvas, DEFAULT_BASE_URL
from .client import CanvasClient
from .errors import (
    CanvasError,
    CanvasAuthError,
    CanvasNotFoundError,
    CanvasHTTPError,
)
from .models import (
    User,
    Course,
    Assignment,
    Module,
    ModuleItem,
    Homework,
)

__all__ = [
    "MvaCanvas",
    "DEFAULT_BASE_URL",
    "CanvasClient",
    "CanvasError",
    "CanvasAuthError",
    "CanvasNotFoundError",
    "CanvasHTTPError",
    "User",
    "Course",
    "Assignment",
    "Module",
    "ModuleItem",
    "Homework",
]

__version__ = "0.1.0"
