"""Fuente real del Boletín Oficial del Estado.

API de datos abiertos, verificada con peticiones reales el 19/10/2026:

* ``GET /datosabiertos/api/boe/sumario/{AAAAMMDD}`` con ``Accept:
  application/json``. Sin autenticación, sin clave y sin registro.
* Devuelve sumarios **por fecha** y no permite buscar, de ahí el índice local.
* Los días sin publicación (domingos y algunos festivos) responden ``404``, y el
  cuerpo llega en XML aunque se pida JSON. Eso **no es un error**: es ausencia de
  publicación.
* Solo admite ``GET``; un ``POST`` devuelve ``403``.
* Su ``robots.txt`` (494 KB) permite el acceso y no excluye a ningún agente de
  IA, pero no es el documento que nos aplica: el permiso viene de la licencia de
  reutilización, así que el consentimiento declarado es ``DATOS_ABIERTOS``.

El texto completo de cada convocatoria se descarga aparte, bajo demanda, desde
``url_xml``. Es el nivel 2 del matching: ese texto se le entrega al modelo para
que lea los requisitos, porque la prosa jurídica no se parsea con expresiones
regulares.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from bs4 import BeautifulSoup, Tag

from convocatorias_mcp.models import Convocatoria
from convocatorias_mcp.sources.base import (
    DEFAULT_MIN_DELAY_SECONDS,
    Consentimiento,
    PoliteHttpSource,
    SinPublicacion,
    SourceError,
)
from convocatorias_mcp.sources.sumario import ParseoSumarioError, parsear_sumario

__all__ = ["BoeSource", "extraer_texto"]

#: Espacios, tabuladores y el espacio duro (U+00A0), que el BOE usa a mansalva.
_ESPACIOS = re.compile("[ \t\u00a0]+")

#: Licencia tipo aprobada por Resolución de la Agencia de 27 de junio de 2024,
#: al amparo de la Ley 37/2007 sobre reutilización de la información del sector
#: público.
LICENCIA_BOE = "https://www.boe.es/informacion/aviso_legal/index.php#reutilizacion"

#: Cita que la licencia obliga a mostrar, con enlace a la sede electrónica.
CITA_BOE = "Fuente de los datos: Agencia Estatal Boletín Oficial del Estado"


def extraer_texto(soup: BeautifulSoup) -> str | None:
    """Saca el texto legible del XML de un documento del BOE.

    Se conservan los párrafos como párrafos porque el texto va a leerlo un
    modelo, y una pared de texto sin separaciones se entiende peor. Las tablas
    se aplanan con separadores: no son bonitas, pero perder sus datos sería
    peor, y en estas convocatorias suelen contener plazos y plazas.
    """
    nodo = soup.find("texto")
    if not isinstance(nodo, Tag):
        return None

    bloques: list[str] = []
    for hijo in nodo.find_all(["p", "table"], recursive=False):
        if hijo.name == "table":
            filas = [
                " | ".join(
                    _limpiar(celda.get_text(" ", strip=True))
                    for celda in fila.find_all(["td", "th"])
                )
                for fila in hijo.find_all("tr")
            ]
            texto = "\n".join(fila for fila in filas if fila.strip(" |"))
        else:
            texto = _limpiar(hijo.get_text(" ", strip=True))
        if texto:
            bloques.append(texto)

    if not bloques:
        # Algún documento no usa <p>: mejor el texto plano que nada.
        plano = _limpiar(nodo.get_text("\n", strip=True))
        return plano or None

    return "\n\n".join(bloques)


def _limpiar(valor: str) -> str:
    """Normaliza los espacios duros que usa el BOE (``\\xa0``)."""
    return _ESPACIOS.sub(" ", valor).strip()


class BoeSource(PoliteHttpSource):
    """El BOE a través de su API de datos abiertos."""

    name = "boe"
    display_name = "Boletín Oficial del Estado"
    base_url = "https://www.boe.es"
    method = "api"
    consentimiento = Consentimiento.DATOS_ABIERTOS
    licencia_url = LICENCIA_BOE
    cita_requerida = CITA_BOE

    #: Ruta de la API de sumarios.
    RUTA_SUMARIO = "/datosabiertos/api/boe/sumario"

    def __init__(
        self,
        *,
        min_delay: float = DEFAULT_MIN_DELAY_SECONDS,
        timeout: float = 30.0,
        **kwargs: object,
    ) -> None:
        # El sumario de un día ronda los 300 KB, así que se da más margen que el
        # de por defecto: un backfill de 90 días no debe caerse por un pico.
        super().__init__(min_delay=min_delay, timeout=timeout, **kwargs)  # type: ignore[arg-type]

    def url_sumario(self, fecha: date) -> str:
        return f"{self.base_url}{self.RUTA_SUMARIO}/{fecha.strftime('%Y%m%d')}"

    async def fetch_dia(self, fecha: date) -> list[Convocatoria]:
        """Devuelve las convocatorias publicadas ese día.

        Un ``404`` se traduce a :class:`SinPublicacion`, que la ingesta registra
        como día sin boletín en vez de como error.
        """
        url = self.url_sumario(fecha)
        try:
            payload = await self.fetch_json(url)
        except SourceError as exc:
            if exc.status_code == 404:
                raise SinPublicacion(self.name, fecha) from exc
            raise

        try:
            convocatorias = parsear_sumario(payload, fuente=self.name, fecha=fecha)
        except ParseoSumarioError as exc:
            raise SourceError(
                self.name,
                f"el sumario del {fecha.isoformat()} no tiene la forma esperada: {exc}",
                cause=exc,
            ) from exc

        if not convocatorias:
            # El BOE no publica sumarios vacíos: si no sale nada, la estructura
            # ha cambiado y es mejor saberlo que guardar un día en blanco.
            raise SourceError(
                self.name,
                f"el sumario del {fecha.isoformat()} se descargó pero no contiene "
                "ninguna convocatoria; probablemente ha cambiado su estructura",
            )
        return convocatorias

    async def fetch_texto(self, convocatoria: Convocatoria) -> Convocatoria:
        """Descarga el texto completo de una convocatoria desde su XML.

        Si ya está descargado no se vuelve a pedir: es la caché que exige el
        trato educado con la fuente, y el texto de un BOE publicado no cambia.
        """
        if convocatoria.texto_descargado:
            return convocatoria

        soup = await self.fetch_soup(convocatoria.url_xml, features="lxml-xml")
        texto = extraer_texto(soup)
        if not texto:
            raise SourceError(
                self.name,
                f"el XML de {convocatoria.identificador} no trae texto legible",
            )

        return convocatoria.model_copy(
            update={"texto_completo": texto, "texto_descargado_at": datetime.now(UTC)}
        )
