"""Fuente de pruebas que lee sumarios guardados en disco.

Existe para poder levantar el servidor MCP completo y probarlo en Claude
Desktop sin depender de la red ni de que el BOE publique hoy: el sumario real
del 17/10/2026 está guardado en ``tests/fixtures/``.

También es la fuente que usan los tests de ingesta, y sirve de ejemplo mínimo
de lo que hay que escribir para añadir un boletín: declarar los metadatos de
clase e implementar ``fetch_dia``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from convocatorias_mcp.models import Convocatoria
from convocatorias_mcp.sources.base import (
    BoletinSource,
    Consentimiento,
    SinPublicacion,
    SourceError,
)
from convocatorias_mcp.sources.sumario import ParseoSumarioError, parsear_sumario

__all__ = ["DIRECTORIO_FIXTURES", "FixtureSource"]

#: Los sumarios de ejemplo viven con los tests, que es su sitio: son datos de
#: prueba, no datos de la aplicación.
DIRECTORIO_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


class FixtureSource(BoletinSource):
    """Sirve sumarios desde ficheros, con la misma interfaz que un boletín real.

    Busca ``boe_sumario_AAAAMMDD.json`` en el directorio de fixtures. Una fecha
    sin fichero se trata como día sin publicación, igual que haría el BOE con un
    domingo, así que la ingesta se comporta igual que con la fuente de verdad.
    """

    name = "fixture"
    display_name = "Sumarios de ejemplo (sin red)"
    base_url = "file://"
    method = "api"
    consentimiento = Consentimiento.DATOS_ABIERTOS
    licencia_url = "https://www.boe.es/informacion/aviso_legal/index.php#reutilizacion"
    cita_requerida = "Fuente de los datos: Agencia Estatal Boletín Oficial del Estado"

    def __init__(self, *, directorio: Path | None = None) -> None:
        self.directorio = directorio or DIRECTORIO_FIXTURES

    def ruta_para(self, fecha: date) -> Path:
        return self.directorio / f"boe_sumario_{fecha.strftime('%Y%m%d')}.json"

    def fechas_disponibles(self) -> list[date]:
        """Fechas para las que hay sumario guardado, de más antigua a más nueva."""
        fechas = []
        for ruta in self.directorio.glob("boe_sumario_*.json"):
            crudo = ruta.stem.removeprefix("boe_sumario_")
            if len(crudo) == 8 and crudo.isdigit():
                fechas.append(date(int(crudo[:4]), int(crudo[4:6]), int(crudo[6:8])))
        return sorted(fechas)

    async def fetch_dia(self, fecha: date) -> list[Convocatoria]:
        ruta = self.ruta_para(fecha)
        if not ruta.is_file():
            raise SinPublicacion(self.name, fecha)

        try:
            payload = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceError(self.name, f"no se pudo leer {ruta}: {exc}", cause=exc) from exc

        try:
            return parsear_sumario(payload, fuente=self.name, fecha=fecha)
        except ParseoSumarioError as exc:
            raise SourceError(
                self.name, f"{ruta} no tiene forma de sumario: {exc}", cause=exc
            ) from exc

    def is_available(self) -> bool:
        return self.directorio.is_dir()
