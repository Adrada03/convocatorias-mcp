"""Contrato que cumple todo portal de empleo, más la infraestructura de
scraping educado que comparten los adaptadores.

El punto central del diseño está aquí: :class:`JobSource` define una única
operación, ``search(filters) -> list[JobListing]``. El aggregator, la caché y
el servidor MCP solo conocen esta interfaz, así que añadir un portal nuevo es
crear un fichero en ``sources/``, implementar ``search()`` y registrarlo.

:class:`PoliteHttpSource` es la clase base para los portales que se consultan
por HTTP (con o sin API) y centraliza los requisitos no negociables de
cortesía, para que ningún adaptador pueda olvidarse de uno:

* ``robots.txt`` comprobado de verdad antes de cada petición, con caché por host
* un retardo mínimo entre peticiones al mismo dominio, compartido entre fuentes
* User-Agent honesto e identificado, nunca disfrazado de navegador
* todo fallo de red o de HTTP convertido en :class:`SourceError`
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from types import TracebackType
from typing import ClassVar, Literal, Self
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from jobscout_mcp import __version__
from jobscout_mcp.models import JobListing, SearchFilters

__all__ = [
    "DEFAULT_MIN_DELAY_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "JobSource",
    "PoliteHttpSource",
    "RateLimiter",
    "RobotsPolicy",
    "SourceError",
    "user_agent",
]

#: Retardo mínimo entre dos peticiones al mismo dominio. Es un suelo, no un
#: objetivo: si ``robots.txt`` pide un ``Crawl-delay`` mayor, gana el del sitio.
DEFAULT_MIN_DELAY_SECONDS = 1.5

DEFAULT_TIMEOUT_SECONDS = 15.0

#: Páginas de resultados que superan este tamaño se consideran sospechosas
#: (una captcha wall o un error servido con 200) y no se intentan parsear.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


def user_agent() -> str:
    """User-Agent del proyecto: dice qué es y dónde reclamar.

    No se imita ningún navegador a propósito. Un administrador que vea esta
    cadena en sus logs puede identificar el cliente y contactar o bloquear.
    """
    return f"jobscout-mcp/{__version__} (+https://github.com/Adrada03/jobscout-mcp)"


class SourceError(Exception):
    """Fallo controlado de una fuente.

    Es la única excepción que un adaptador debe dejar salir de ``search()``.
    El aggregator la captura por fuente y la traduce a
    ``source_status[fuente] = "error: ..."``, de modo que una fuente caída
    nunca rompe la búsqueda completa. La excepción original se conserva en
    ``__cause__`` y en :attr:`cause` para diagnóstico, pero no se propaga.
    """

    def __init__(self, source: str, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.message = message
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:
        return f"[{self.source}] {self.message}"


# --------------------------------------------------------------------------- #
# Rate limiting por dominio
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Impone un retardo mínimo entre peticiones al mismo host.

    El estado es de proceso y no de instancia: dos adaptadores que apunten al
    mismo dominio (o varias búsquedas concurrentes de la misma fuente) deben
    compartir la cola, que es justo lo que un servidor espera de un cliente
    educado.
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


class JobSource(ABC):
    """Un portal de empleo consultable.

    Implementar un portal nuevo son tres cosas: declarar los metadatos de
    clase, implementar :meth:`search` y registrar la clase. Nada más; ni el
    aggregator ni la caché ni el servidor MCP necesitan cambiar.

    Contrato que se espera de :meth:`search`:

    * Devuelve como máximo ``filters.limit_per_source`` ofertas.
    * Traduce a la query del portal los filtros que este sepa aplicar y deja
      los demás al aggregator, que vuelve a filtrar con
      ``SearchFilters.matches``.
    * Rellena solo los campos que el portal publique de verdad; lo que no
      encuentre se queda en ``None``, nunca inventado.
    * Ante cualquier fallo (red, HTML cambiado, bloqueo) lanza
      :class:`SourceError` y nada más: la excepción original va en ``cause``.
    * Una página de resultados vacía es una lista vacía, no un error.
    """

    #: Identificador corto y estable. Es lo que el usuario escribe en el
    #: parámetro ``sources`` de los tools MCP y lo que se guarda en la caché.
    name: ClassVar[str]

    #: Nombre del portal tal y como se le presenta al usuario.
    display_name: ClassVar[str]

    #: Origen del portal, para construir URLs absolutas y consultar robots.txt.
    base_url: ClassVar[str]

    #: Cómo se obtienen los datos. Se publica en la tabla de fuentes del README
    #: y en la respuesta de ``compare_sources``.
    method: ClassVar[Literal["api", "scraping"]]

    #: ``True`` si el portal exige credenciales o alta como partner. Las
    #: fuentes así se marcan ``skipped`` cuando no hay credenciales en lugar de
    #: dar error.
    requires_credentials: ClassVar[bool] = False

    @abstractmethod
    async def search(self, filters: SearchFilters) -> list[JobListing]:
        """Busca ofertas en el portal y las devuelve normalizadas."""
        raise NotImplementedError

    async def fetch_details(self, listing: JobListing) -> JobListing:
        """Enriquece una oferta con su descripción completa.

        Por defecto devuelve la oferta tal cual: muchos portales ya dan en la
        página de resultados todo lo que sabemos extraer, y pedir el detalle
        tiene un coste en peticiones. Los adaptadores que puedan aportar más lo
        sobrescriben.
        """
        return listing

    def is_available(self) -> bool:
        """``True`` si la fuente se puede consultar en esta instalación.

        El aggregator marca ``skipped`` (no ``error``) las fuentes que
        devuelvan ``False``, por ejemplo una API cuyas credenciales no están
        configuradas.
        """
        return True

    async def aclose(self) -> None:
        """Libera los recursos de la fuente. Por defecto no hay ninguno."""
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} method={self.method!r}>"


# --------------------------------------------------------------------------- #
# Base para fuentes HTTP
# --------------------------------------------------------------------------- #


class PoliteHttpSource(JobSource):
    """Base para fuentes que se consultan por HTTP, con cortesía incluida.

    Un adaptador que herede de aquí y use :meth:`fetch_soup` o
    :meth:`fetch_text` cumple sin esfuerzo los requisitos de ``robots.txt``,
    rate limiting y User-Agent: no hay forma de saltárselos por descuido.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        min_delay: float = DEFAULT_MIN_DELAY_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        respect_robots: bool = True,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._min_delay = min_delay
        self._respect_robots = respect_robots
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
        """Convierte un href relativo del portal en URL absoluta."""
        return urljoin(self.base_url, path)

    async def fetch_text(self, url: str, *, params: dict[str, str | int] | None = None) -> str:
        """Pide una página respetando robots.txt y el rate limit.

        Cualquier problema sale como :class:`SourceError`; esta función no deja
        escapar excepciones de ``httpx``.
        """
        if self._respect_robots and not await self._robots_allows(url):
            raise SourceError(self.name, f"robots.txt no permite rastrear {url}")

        delay = self._min_delay
        if self._respect_robots:
            declared = await self._robots_crawl_delay(url)
            if declared is not None:
                delay = max(delay, declared)

        await RateLimiter.acquire(urlsplit(url).netloc, delay)

        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SourceError(
                self.name,
                f"HTTP {exc.response.status_code} al pedir {exc.request.url}",
                cause=exc,
            ) from exc
        except httpx.RequestError as exc:
            raise SourceError(self.name, f"fallo de red al pedir {url}: {exc}", cause=exc) from exc

        if len(response.content) > MAX_RESPONSE_BYTES:
            raise SourceError(self.name, f"respuesta inesperadamente grande desde {url}")
        return response.text

    async def fetch_soup(
        self, url: str, *, params: dict[str, str | int] | None = None
    ) -> BeautifulSoup:
        """Como :meth:`fetch_text`, pero devuelve el HTML ya parseado con lxml."""
        html = await self.fetch_text(url, params=params)
        try:
            return BeautifulSoup(html, "lxml")
        except Exception as exc:  # pragma: no cover - lxml roto en la instalación
            raise SourceError(self.name, f"no se pudo parsear el HTML de {url}", cause=exc) from exc

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
