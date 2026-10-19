"""Contrato que cumple todo boletín oficial, más la infraestructura de acceso
educado que comparten las fuentes.

El punto central del diseño está aquí: :class:`BoletinSource` define una
operación principal, ``fetch_dia(fecha) -> list[Convocatoria]``. La ingesta, el
índice y el servidor MCP solo conocen esta interfaz, así que añadir un boletín
autonómico es crear un fichero, implementar ``fetch_dia()`` y registrarlo.

:class:`PoliteHttpSource` es la clase base para los boletines que se consultan
por HTTP y centraliza los requisitos de cortesía, para que ninguna fuente pueda
olvidarse de uno:

* un retardo mínimo entre peticiones al mismo dominio, compartido entre fuentes
* User-Agent honesto e identificado, nunca disfrazado de navegador
* ``robots.txt`` comprobado de verdad **en las fuentes que se apoyan en él**
  (ver :class:`Consentimiento`)
* todo fallo de red o de HTTP convertido en :class:`SourceError`
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from datetime import date
from enum import StrEnum
from types import TracebackType
from typing import Any, ClassVar, Literal, Self
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from convocatorias_mcp import __version__
from convocatorias_mcp.models import Convocatoria

__all__ = [
    "DEFAULT_MIN_DELAY_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "BoletinSource",
    "Consentimiento",
    "PoliteHttpSource",
    "RateLimiter",
    "RobotsPolicy",
    "SinPublicacion",
    "SourceError",
    "user_agent",
]

#: Retardo mínimo entre dos peticiones al mismo dominio. Es un suelo, no un
#: objetivo: si ``robots.txt`` pide un ``Crawl-delay`` mayor, gana el del sitio.
DEFAULT_MIN_DELAY_SECONDS = 1.5

DEFAULT_TIMEOUT_SECONDS = 20.0

#: Respuestas por encima de este tamaño se consideran sospechosas y no se
#: intentan parsear. Un sumario del BOE ronda los 300 KB.
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


def user_agent() -> str:
    """User-Agent del proyecto: dice qué es y dónde reclamar.

    No se imita ningún navegador a propósito. Un administrador que vea esta
    cadena en sus logs puede identificar el cliente y contactar o bloquear.
    """
    return f"convocatorias-mcp/{__version__} (+https://github.com/Adrada03/convocatorias-mcp)"


class Consentimiento(StrEnum):
    """De dónde viene el permiso para leer una fuente.

    Es la política de fuentes del proyecto convertida en código. Cada fuente
    declara su origen de consentimiento y :class:`PoliteHttpSource` solo
    comprueba ``robots.txt`` cuando ese es el permiso en el que se apoya:
    ``robots.txt`` se dirige a rastreadores que descubren contenido siguiendo
    enlaces, y no es el documento que regula a un cliente de una API publicada
    para terceros ni a un reutilizador de datos abiertos.
    """

    #: Datos abiertos con licencia de reutilización explícita (el caso del BOE).
    DATOS_ABIERTOS = "datos_abiertos"
    #: API documentada para terceros, con o sin credenciales.
    API_PUBLICA = "api_publica"
    #: Feed RSS/JSON publicado para ser consumido.
    FEED_PUBLICO = "feed_publico"
    #: Sin API ni feed: el permiso es lo que diga ``robots.txt``.
    ROBOTS = "robots"

    @property
    def exige_robots(self) -> bool:
        return self is Consentimiento.ROBOTS


class SourceError(Exception):
    """Fallo controlado de una fuente.

    Es la única excepción que una fuente debe dejar salir, junto con
    :class:`SinPublicacion`. La ingesta la captura por fuente y por día y la
    registra como ``EstadoIngesta.ERROR``, de modo que un boletín caído no
    rompe la ingesta completa. La excepción original se conserva en
    ``__cause__`` y en :attr:`cause`, pero no se propaga.
    """

    def __init__(
        self,
        source: str,
        message: str,
        *,
        cause: BaseException | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.message = message
        self.cause = cause
        #: Código HTTP, cuando el fallo venía de una respuesta. Permite a una
        #: fuente distinguir casos como el 404 que el BOE devuelve los días que
        #: no publica.
        self.status_code = status_code
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:
        return f"[{self.source}] {self.message}"


class SinPublicacion(Exception):
    """El boletín no publicó ese día. **No es un error.**

    El BOE no publica los domingos ni algunos festivos, y responde 404 a esas
    fechas. Esa información es válida y hay que registrarla como
    ``EstadoIngesta.SIN_PUBLICACION``: no debe ensuciar la salud de la fuente ni
    hacer que la ingesta reintente la fecha para siempre.

    Es una excepción aparte de :class:`SourceError`, y no un valor de retorno,
    para que no se pueda confundir con "publicó pero no había nada".
    """

    def __init__(self, source: str, fecha: date) -> None:
        super().__init__(f"{source} no publicó el {fecha.isoformat()}")
        self.source = source
        self.fecha = fecha


# --------------------------------------------------------------------------- #
# Rate limiting por dominio
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Impone un retardo mínimo entre peticiones al mismo host.

    El estado es de proceso y no de instancia: dos fuentes que apunten al mismo
    dominio (o un backfill concurrente) deben compartir la cola, que es lo que
    un servidor espera de un cliente educado.
    """

    _locks: ClassVar[dict[tuple[int, str], asyncio.Lock]] = {}
    _last_request: ClassVar[dict[str, float]] = {}

    @classmethod
    def _lock_for(cls, host: str) -> asyncio.Lock:
        # La clave incluye el id del event loop en curso: los locks de asyncio
        # se atan al loop en el que se usan y los tests crean uno por caso.
        key = (id(asyncio.get_running_loop()), host)
        lock = cls._locks.get(key)
        if lock is None:
            lock = cls._locks[key] = asyncio.Lock()
        return lock

    @classmethod
    async def acquire(cls, host: str, min_delay: float = DEFAULT_MIN_DELAY_SECONDS) -> None:
        """Espera lo que haga falta para no pisar el retardo mínimo de ``host``."""
        if min_delay <= 0:
            return
        async with cls._lock_for(host):
            previous = cls._last_request.get(host)
            now = time.monotonic()
            if previous is not None:
                wait_for = min_delay - (now - previous)
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
            cls._last_request[host] = time.monotonic()

    @classmethod
    def reset(cls) -> None:
        """Limpia el estado. Pensado para los tests, no para producción."""
        cls._locks.clear()
        cls._last_request.clear()


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


class RobotsPolicy:
    """Lee y aplica el ``robots.txt`` de cada host, con caché por proceso.

    Se sigue el criterio de la RFC 9309 ante un ``robots.txt`` que no se puede
    leer:

    * ``404``/``410`` y otros 4xx: no hay reglas, se permite rastrear.
    * ``401``/``403``: el sitio restringe el propio ``robots.txt``, se
      interpreta como prohibición total.
    * ``5xx`` o error de red: no se puede saber, así que se prohíbe. Preferimos
      devolver un ``SourceError`` legible a rastrear a ciegas.
    """

    _cache: ClassVar[dict[str, RobotFileParser | None]] = {}

    def __init__(self, *, agent: str | None = None) -> None:
        self.agent = agent or user_agent()

    @staticmethod
    def origin_of(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    async def _parser_for(self, client: httpx.AsyncClient, url: str) -> RobotFileParser | None:
        """Devuelve el parser del host, o ``None`` si está prohibido todo."""
        origin = self.origin_of(url)
        if origin in self._cache:
            return self._cache[origin]

        parser = RobotFileParser()
        robots_url = urljoin(origin + "/", "robots.txt")
        try:
            response = await client.get(robots_url)
        except httpx.RequestError:
            self._cache[origin] = None
            return None

        if response.status_code in (401, 403):
            self._cache[origin] = None
            return None
        if response.status_code >= 500:
            self._cache[origin] = None
            return None
        if response.status_code >= 400:
            # Igual que urllib ante un 404: no hay reglas, todo permitido.
            parser.allow_all = True
        else:
            parser.parse(response.text.splitlines())

        # ``parse()`` no marca el fichero como leído, y tanto ``can_fetch`` como
        # ``crawl_delay`` lo exigen: sin esto el parser prohibiría todo y nunca
        # reportaría el Crawl-delay del sitio.
        parser.modified()

        self._cache[origin] = parser
        return parser

    async def allows(self, client: httpx.AsyncClient, url: str) -> bool:
        """``True`` si nuestro User-Agent puede pedir ``url``."""
        parser = await self._parser_for(client, url)
        if parser is None:
            return False
        return parser.can_fetch(self.agent, url)

    async def crawl_delay(self, client: httpx.AsyncClient, url: str) -> float | None:
        """``Crawl-delay`` declarado por el host, si declara alguno."""
        parser = await self._parser_for(client, url)
        if parser is None:
            return None
        delay = parser.crawl_delay(self.agent)
        return float(delay) if delay is not None else None

    @classmethod
    def reset_cache(cls) -> None:
        """Olvida los ``robots.txt`` cacheados. Pensado para los tests."""
        cls._cache.clear()


# --------------------------------------------------------------------------- #
# La interfaz
# --------------------------------------------------------------------------- #


class BoletinSource(ABC):
    """Un boletín oficial consultable.

    Implementar un boletín nuevo son tres cosas: declarar los metadatos de
    clase, implementar :meth:`fetch_dia` y registrar la clase. Nada más; ni la
    ingesta ni el índice ni el servidor MCP necesitan cambiar.

    Contrato que se espera de :meth:`fetch_dia`:

    * Devuelve las convocatorias publicadas **ese día**, ya normalizadas.
    * Si el boletín no publicó ese día, lanza :class:`SinPublicacion`. No
      devuelve una lista vacía, que significaría "publicó y no había nada".
    * Rellena solo los campos que el boletín publique de verdad; lo que no
      encuentre se queda en ``None``, nunca inventado. En particular, no deduce
      un plazo que no esté escrito.
    * Ante cualquier otro fallo (red, estructura cambiada) lanza
      :class:`SourceError` y nada más: la excepción original va en ``cause``.
    * No descarga textos completos: eso es :meth:`fetch_texto`, bajo demanda.
    """

    #: Identificador corto y estable. Es lo que el usuario escribe en los tools
    #: MCP y lo que se guarda en el índice.
    name: ClassVar[str]

    #: Nombre del boletín tal y como se le presenta al usuario.
    display_name: ClassVar[str]

    #: Origen del boletín, para construir URLs absolutas.
    base_url: ClassVar[str]

    #: Cómo se obtienen los datos. Se publica en la tabla de fuentes del README.
    method: ClassVar[Literal["api", "scraping"]]

    #: De dónde viene el permiso para leer esta fuente.
    consentimiento: ClassVar[Consentimiento]

    #: URL de la licencia de reutilización, cuando la fuente publica una.
    licencia_url: ClassVar[str | None] = None

    #: Cita exacta que la licencia obliga a mostrar, si obliga a alguna. Las
    #: condiciones de reutilización del BOE, por ejemplo, exigen citar la
    #: fuente con un enlace a su sede electrónica. Se declara aquí, y no en la
    #: plantilla del digest o del dashboard, para que cumplirla no dependa de
    #: que alguien se acuerde: quien añada una fuente la declara una vez y
    #: todas las salidas la arrastran.
    cita_requerida: ClassVar[str | None] = None

    @abstractmethod
    async def fetch_dia(self, fecha: date) -> list[Convocatoria]:
        """Devuelve las convocatorias publicadas por el boletín ese día."""
        raise NotImplementedError

    async def fetch_texto(self, convocatoria: Convocatoria) -> Convocatoria:
        """Descarga el texto completo de una convocatoria.

        Por defecto devuelve la convocatoria tal cual. Es el nivel 2 del
        matching: el texto íntegro se le da al modelo para que lea los
        requisitos, porque esa prosa jurídica no se parsea con expresiones
        regulares.
        """
        return convocatoria

    def is_available(self) -> bool:
        """``True`` si la fuente se puede consultar en esta instalación."""
        return True

    async def aclose(self) -> None:
        """Libera los recursos de la fuente. Por defecto no hay ninguno."""
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} method={self.method!r}>"


# --------------------------------------------------------------------------- #
# Base para fuentes HTTP
# --------------------------------------------------------------------------- #


class PoliteHttpSource(BoletinSource):
    """Base para boletines que se consultan por HTTP, con cortesía incluida.

    Una fuente que herede de aquí y use :meth:`fetch_json`, :meth:`fetch_text`
    o :meth:`fetch_soup` cumple sin esfuerzo el rate limiting, el User-Agent y
    —cuando su consentimiento se apoya en él— ``robots.txt``.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        min_delay: float = DEFAULT_MIN_DELAY_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        respect_robots: bool | None = None,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._min_delay = min_delay
        # Por defecto, lo que diga el consentimiento declarado por la fuente.
        # El parámetro existe para los tests y para forzarlo a mano.
        self._respect_robots = (
            self.consentimiento.exige_robots if respect_robots is None else respect_robots
        )
        self._robots = RobotsPolicy()

    # --- Cliente HTTP ----------------------------------------------------- #

    @property
    def client(self) -> httpx.AsyncClient:
        """Cliente ``httpx`` perezoso, ya configurado con el User-Agent honesto."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": user_agent(),
                    "Accept-Language": "es-ES,es;q=0.9",
                },
            )
        return self._client

    async def aclose(self) -> None:
        """Cierra el cliente, pero solo si lo creó esta instancia."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # --- Peticiones ------------------------------------------------------- #

    def absolute_url(self, path: str) -> str:
        """Convierte un href relativo del boletín en URL absoluta."""
        return urljoin(self.base_url, path)

    async def fetch_text(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """Pide una página respetando el rate limit (y robots.txt si aplica).

        Cualquier problema sale como :class:`SourceError`, con el código HTTP en
        ``status_code`` para que la fuente pueda distinguir casos como el 404 de
        un día sin publicación.
        """
        if self._respect_robots and not await self._robots_allows(url):
            raise SourceError(self.name, f"robots.txt no permite acceder a {url}")

        delay = self._min_delay
        if self._respect_robots:
            declared = await self._robots_crawl_delay(url)
            if declared is not None:
                delay = max(delay, declared)

        await RateLimiter.acquire(urlsplit(url).netloc, delay)

        try:
            response = await self.client.get(url, params=params, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SourceError(
                self.name,
                f"HTTP {exc.response.status_code} al pedir {url}",
                cause=exc,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise SourceError(self.name, f"fallo de red al pedir {url}: {exc}", cause=exc) from exc

        if len(response.content) > MAX_RESPONSE_BYTES:
            raise SourceError(self.name, f"respuesta inesperadamente grande desde {url}")
        return response.text

    async def fetch_json(self, url: str, *, params: dict[str, str | int] | None = None) -> Any:
        """Como :meth:`fetch_text`, pero devuelve el JSON ya parseado.

        Pide JSON explícitamente por cabecera. Si la respuesta no es JSON, da
        :class:`SourceError`: el BOE, por ejemplo, contesta XML en los errores
        aunque le pidas JSON, y parsear eso a ciegas daría un fallo confuso.
        """
        texto = await self.fetch_text(url, params=params, headers={"Accept": "application/json"})
        try:
            return json.loads(texto)
        except json.JSONDecodeError as exc:
            raise SourceError(
                self.name, f"la respuesta de {url} no es JSON válido", cause=exc
            ) from exc

    async def fetch_soup(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        features: str = "lxml",
    ) -> BeautifulSoup:
        """Como :meth:`fetch_text`, pero devuelve el documento ya parseado.

        ``features="lxml-xml"`` para los documentos XML de un boletín.
        """
        contenido = await self.fetch_text(url, params=params)
        try:
            return BeautifulSoup(contenido, features)
        except Exception as exc:  # pragma: no cover - parser roto en la instalación
            raise SourceError(self.name, f"no se pudo parsear {url}", cause=exc) from exc

    async def _robots_allows(self, url: str) -> bool:
        try:
            return await self._robots.allows(self.client, url)
        except httpx.RequestError as exc:  # pragma: no cover - red caída
            raise SourceError(self.name, f"no se pudo leer robots.txt: {exc}", cause=exc) from exc

    async def _robots_crawl_delay(self, url: str) -> float | None:
        try:
            return await self._robots.crawl_delay(self.client, url)
        except httpx.RequestError:  # pragma: no cover - ya cubierto por _robots_allows
            return None
