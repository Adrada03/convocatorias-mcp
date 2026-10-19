"""Fuentes de boletines oficiales.

Cada módulo de este paquete implementa
:class:`convocatorias_mcp.sources.base.BoletinSource` para un boletín concreto.
Nada fuera de este paquete conoce detalles de ningún boletín: la ingesta, el
índice y el servidor MCP solo hablan con la interfaz.
"""

from convocatorias_mcp.sources.base import (
    DEFAULT_MIN_DELAY_SECONDS,
    BoletinSource,
    Consentimiento,
    PoliteHttpSource,
    RateLimiter,
    RobotsPolicy,
    SinPublicacion,
    SourceError,
    user_agent,
)

__all__ = [
    "DEFAULT_MIN_DELAY_SECONDS",
    "BoletinSource",
    "Consentimiento",
    "PoliteHttpSource",
    "RateLimiter",
    "RobotsPolicy",
    "SinPublicacion",
    "SourceError",
    "user_agent",
]
