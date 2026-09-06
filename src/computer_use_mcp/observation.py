"""Observation capture orchestration: full Observation building plus a stable digest.

Layering (master-mission section 5): this module imports only ``backend`` and ``models``
— never ``verification``/``agent``/``server``. The digest is computed locally and matches
the historical ``VerificationEngine.observation_digest`` value, so the
``computer_observe`` tool output shape (``{"observation": ..., "digest": ...}``) is
unchanged for external MCP clients.
"""

from __future__ import annotations

import hashlib

from .backend import ComputerBackend
from .models import Observation


def observation_digest(observation: Observation) -> str:
    """Stable sha256 digest of an observation's base64 screenshot payload."""
    return hashlib.sha256(observation.image_base64.encode("ascii")).hexdigest()


class ObservationEngine:
    """Coordinates computer-state capture independently from action execution."""

    def __init__(self, backend: ComputerBackend) -> None:
        self.backend = backend

    def capture(self) -> Observation:
        """Delegate a full Observation capture to the backend (identity, timing, coordinates)."""
        return self.backend.observe()

    def capture_with_digest(self) -> tuple[Observation, str]:
        """Capture one observation plus its stable digest (the ``computer_observe`` contract)."""
        observation = self.capture()
        return observation, observation_digest(observation)
