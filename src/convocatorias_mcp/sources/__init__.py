"""Fuentes de boletines oficiales y su registro.

Cada módulo de este paquete implementa
:class:`convocatorias_mcp.sources.base.BoletinSource` para un boletín concreto.
Nada fuera de este paquete conoce detalles de ningún boletín: la ingesta, el
índice y el servidor MCP solo hablan con la interfaz.

El registro es una **lista explícita** y no un decorador que se autorregistre:
así se lee de un vistazo qué fuentes existen, el import es visible y añadir un
boletín es una línea aquí. Añadir una fuente nueva son tres pasos:

1. Crear el módulo e implementar ``fetch_dia``.
2. Añadir la clase a :data:`FUENTES`.
3. Escribir sus tests con ``respx``, sin tocar la red.
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
from convocatorias_mcp.sources.boe import BoeSource
from convocatorias_mcp.sources.fixture import FixtureSource

__all__ = [
    "DEFAULT_MIN_DELAY_SECONDS",
    "FUENTES",
    "FUENTE_POR_DEFECTO",
    "BoeSource",
    "BoletinSource",
    "Consentimiento",
    "FixtureSource",
    "PoliteHttpSource",
    "RateLimiter",
    "RobotsPolicy",
    "SinPublicacion",
    "SourceError",
    "crear_fuente",
    "nombres_de_fuentes",
    "user_agent",
]

#: Fuentes disponibles, por nombre.
#:
#: ``fixture`` se queda en el registro a propósito: es la fuente de desarrollo,
#: y permite probar el proyecto entero sin red ni depender de que el BOE haya
#: publicado hoy.
FUENTES: dict[str, type[BoletinSource]] = {
    BoeSource.name: BoeSource,
    FixtureSource.name: FixtureSource,
}

#: La que usa ``ingest`` si no se le dice otra.
FUENTE_POR_DEFECTO = BoeSource.name


def nombres_de_fuentes() -> list[str]:
    """Nombres de las fuentes registradas, en orden estable."""
    return list(FUENTES)


def crear_fuente(nombre: str, **kwargs: object) -> BoletinSource:
    """Instancia una fuente por su nombre.

    Lanza ``KeyError`` con las opciones válidas si el nombre no existe, para que
    el error que ve el usuario sea accionable y no un ``None`` silencioso.
    """
    try:
        clase = FUENTES[nombre]
    except KeyError as exc:
        disponibles = ", ".join(nombres_de_fuentes())
        raise KeyError(
            f"no existe la fuente {nombre!r}; las disponibles son: {disponibles}"
        ) from exc
    return clase(**kwargs)  # type: ignore[arg-type]
