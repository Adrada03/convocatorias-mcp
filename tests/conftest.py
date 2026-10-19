"""Fixtures compartidas por toda la suite.

Regla de la suite: ningún test toca la red. Las fuentes HTTP se prueban con
``respx``, y los parsers con sumarios reales del BOE guardados en
``tests/fixtures/``.

Esa regla no se deja a la buena voluntad: :func:`_sin_red` bloquea el
transporte de ``httpx`` en todos los tests, así que un descuido falla con un
mensaje claro en vez de salir a internet y tardar minutos. Pasó de verdad: al
poner el BOE como fuente por defecto, los tests del comando ``ingest`` se
fueron a la API real y la suite subió de 7 a 94 segundos.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from convocatorias_mcp.models import Convocatoria, Seccion
from convocatorias_mcp.sources.base import RateLimiter, RobotsPolicy

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _clean_shared_state() -> None:
    """El rate limiter y la caché de robots.txt son estado de proceso.

    Se limpian antes de cada test para que el orden de ejecución no influya.
    """
    RateLimiter.reset()
    RobotsPolicy.reset_cache()


@pytest.fixture(autouse=True)
def _sin_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bloquea cualquier petición que llegue de verdad a la red.

    El corte va en la resolución de nombres, que es lo más abajo que se puede
    poner sin tocar el sistema operativo. No vale hacerlo en los transportes de
    ``httpx`` ni en ``httpcore``: ``respx`` parchea justo esas capas, y los dos
    cortafuegos se pisarían. Así, un test con ``respx`` nunca llega hasta aquí y
    uno sin mock falla antes de abrir un socket.
    """

    def bloquear(host: object, port: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError(
            f"un test ha intentado salir a la red: {host}:{port}\n"
            "Simula la respuesta con respx, o usa la fuente 'fixture'."
        )

    monkeypatch.setattr(socket, "getaddrinfo", bloquear)


@pytest.fixture(scope="session")
def sumario_boe() -> dict:
    """Sumario real del BOE del 17/10/2026, tal como lo devolvió la API."""
    return json.loads((FIXTURES / "boe_sumario_20261017.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def sumario_sin_publicacion() -> str:
    """Respuesta real de la API para un domingo: 404 con cuerpo XML."""
    return (FIXTURES / "boe_sumario_sin_publicacion.json").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def documento_boe() -> str:
    """XML real de un documento del BOE, con su texto completo."""
    return (FIXTURES / "boe_documento.xml").read_text(encoding="utf-8")


@pytest.fixture
def convocatoria_factory():
    """Crea convocatorias con valores por defecto razonables.

    Cada test sobrescribe solo los campos que le importan, lo que mantiene los
    tests legibles y resistentes a que el modelo gane campos.
    """

    def _make(**overrides: object) -> Convocatoria:
        data: dict[str, object] = {
            "fuente": "boe",
            "identificador": "BOE-A-2026-21635",
            "titulo": "Resolución por la que se convoca la provisión de puesto de trabajo",
            "seccion": Seccion.II_B,
            "seccion_nombre": "II. Autoridades y personal. - B. Oposiciones y concursos",
            "departamento": "MINISTERIO DE DEFENSA",
            "epigrafe": "Procedimientos de libre designación",
            "control": "2026/15820",
            "fecha_publicacion": date(2026, 10, 17),
            "url_html": "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-21635",
            "url_xml": "https://www.boe.es/diario_boe/xml.php?id=BOE-A-2026-21635",
            "url_pdf": "https://www.boe.es/boe/dias/2026/10/17/pdfs/BOE-A-2026-21635.pdf",
            "ingerido_at": datetime(2026, 10, 19, 10, 0, tzinfo=UTC),
        }
        data.update(overrides)
        return Convocatoria(**data)  # type: ignore[arg-type]

    return _make
