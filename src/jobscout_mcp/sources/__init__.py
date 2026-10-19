"""Adaptadores de portales de empleo.

Cada módulo de este paquete implementa :class:`jobscout_mcp.sources.base.JobSource`
para un portal concreto. Nada fuera de este paquete conoce detalles de ningún
portal: el aggregator, la caché y el servidor MCP solo hablan con la interfaz.
"""

from jobscout_mcp.sources.base import (
    DEFAULT_MIN_DELAY_SECONDS,
    JobSource,
    PoliteHttpSource,
    RateLimiter,
    RobotsPolicy,
    SourceError,
    user_agent,
)

__all__ = [
    "DEFAULT_MIN_DELAY_SECONDS",
    "JobSource",
    "PoliteHttpSource",
    "RateLimiter",
    "RobotsPolicy",
    "SourceError",
    "user_agent",
]
