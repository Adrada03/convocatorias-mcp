"""Tests del contrato de fuente y del scraping educado.

El scraping educado es un requisito, así que se verifica con tests y no solo
con documentación: robots.txt se obedece de verdad, el retardo entre
peticiones existe, el User-Agent se identifica y ningún error de red escapa
sin convertirse en ``SourceError``.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from jobscout_mcp.models import JobListing, SearchFilters
from jobscout_mcp.sources.base import (
    DEFAULT_MIN_DELAY_SECONDS,
    JobSource,
    PoliteHttpSource,
    RateLimiter,
    SourceError,
    user_agent,
)

ROBOTS_URL = "https://example.test/robots.txt"
SEARCH_URL = "https://example.test/ofertas"
ROBOTS_PERMISIVO = "User-agent: *\nDisallow: /admin\n"


class DummySource(PoliteHttpSource):
    """Fuente mínima: lo justo que debe escribir quien añada un portal."""

    name = "dummy"
    display_name = "Dummy"
    base_url = "https://example.test"
    method = "scraping"

    async def search(self, filters: SearchFilters) -> list[JobListing]:
        soup = await self.fetch_soup(SEARCH_URL, params={"q": filters.keywords})
        return [
            JobListing(
                source=self.name,
                source_url=self.absolute_url(article.a["href"]),
                title=article.a.get_text(strip=True),
                company=article["data-company"],
            )
            for article in soup.select("article")
        ]


RESULTADOS_HTML = """
<html><body>
  <article data-company="Acme S.L."><a href="/oferta/rf-1">Backend Python</a></article>
  <article data-company="Globant"><a href="/oferta/rf-2">DevOps Engineer</a></article>
</body></html>
"""


# --------------------------------------------------------------------------- #
# El contrato
# --------------------------------------------------------------------------- #


def test_no_se_puede_instanciar_una_fuente_sin_search():
    class Incompleta(JobSource):
        name = "incompleta"
        display_name = "Incompleta"
        base_url = "https://example.test"
        method = "scraping"

    with pytest.raises(TypeError):
        Incompleta()  # type: ignore[abstract]


async def test_fetch_details_por_defecto_devuelve_la_oferta_tal_cual(listing_factory):
    source = DummySource()
    original = listing_factory()
    assert await source.fetch_details(original) is original


def test_una_fuente_esta_disponible_por_defecto():
    assert DummySource().is_available()


def test_source_error_identifica_la_fuente_y_conserva_la_causa():
    causa = httpx.ConnectTimeout("timeout")
    error = SourceError("dummy", "fallo de red", cause=causa)
    assert str(error) == "[dummy] fallo de red"
    assert error.source == "dummy"
    assert error.__cause__ is causa


# --------------------------------------------------------------------------- #
# User-Agent
# --------------------------------------------------------------------------- #


def test_el_user_agent_es_honesto_y_contactable():
    agent = user_agent()
    assert agent.startswith("jobscout-mcp/")
    assert "github.com/Adrada03/jobscout-mcp" in agent
    # No imitamos ningún navegador.
    for impostor in ("Mozilla", "Chrome", "Safari", "AppleWebKit"):
        assert impostor not in agent


@respx.mock
async def test_las_peticiones_envian_el_user_agent_del_proyecto():
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ROBOTS_PERMISIVO))
    ruta = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=RESULTADOS_HTML))

    async with DummySource(min_delay=0) as source:
        await source.search(SearchFilters(keywords="python"))

    assert ruta.calls.last.request.headers["user-agent"] == user_agent()


# --------------------------------------------------------------------------- #
# Camino feliz
# --------------------------------------------------------------------------- #


@respx.mock
async def test_search_parsea_los_resultados_y_construye_urls_absolutas():
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ROBOTS_PERMISIVO))
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=RESULTADOS_HTML))

    async with DummySource(min_delay=0) as source:
        ofertas = await source.search(SearchFilters(keywords="python"))

    assert [oferta.title for oferta in ofertas] == ["Backend Python", "DevOps Engineer"]
    assert ofertas[0].source_url == "https://example.test/oferta/rf-1"
    assert ofertas[0].seen_in_sources == ["dummy"]


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


@respx.mock
async def test_robots_disallow_impide_la_peticion():
    respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /ofertas\n")
    )
    ruta = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=RESULTADOS_HTML))

    async with DummySource(min_delay=0) as source:
        with pytest.raises(SourceError, match=r"robots.txt"):
            await source.search(SearchFilters(keywords="python"))

    # Lo importante: la oferta no se pidió nunca.
    assert not ruta.called


@respx.mock
async def test_robots_prohibido_solo_para_nuestro_agente():
    respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(
            200, text="User-agent: jobscout-mcp\nDisallow: /\n\nUser-agent: *\nDisallow:\n"
        )
    )
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=RESULTADOS_HTML))

    async with DummySource(min_delay=0) as source:
        with pytest.raises(SourceError, match=r"robots.txt"):
            await source.search(SearchFilters(keywords="python"))


@respx.mock
async def test_sin_robots_txt_se_permite_rastrear():
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(404))
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=RESULTADOS_HTML))

    async with DummySource(min_delay=0) as source:
        assert len(await source.search(SearchFilters(keywords="python"))) == 2


@respx.mock
@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_robots_ilegible_se_interpreta_como_prohibicion(status):
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(status))
    ruta = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=RESULTADOS_HTML))

    async with DummySource(min_delay=0) as source:
        with pytest.raises(SourceError, match=r"robots.txt"):
            await source.search(SearchFilters(keywords="python"))

    assert not ruta.called


@respx.mock
async def test_robots_se_pide_una_sola_vez_por_host():
    ruta_robots = respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text=ROBOTS_PERMISIVO)
    )
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=RESULTADOS_HTML))

    async with DummySource(min_delay=0) as source:
        await source.search(SearchFilters(keywords="python"))
        await source.search(SearchFilters(keywords="java"))

    assert ruta_robots.call_count == 1


@respx.mock
async def test_el_crawl_delay_del_sitio_gana_si_es_mayor():
    respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 9\nDisallow:\n")
    )
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=RESULTADOS_HTML))

    source = DummySource(min_delay=DEFAULT_MIN_DELAY_SECONDS)
    try:
        delay = await source._robots_crawl_delay(SEARCH_URL)
    finally:
        await source.aclose()

    assert delay == 9.0
    assert max(DEFAULT_MIN_DELAY_SECONDS, delay) == 9.0


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


def test_el_retardo_minimo_por_defecto_cumple_el_requisito():
    assert DEFAULT_MIN_DELAY_SECONDS >= 1.5


@respx.mock
async def test_dos_peticiones_al_mismo_host_se_espacian():
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ROBOTS_PERMISIVO))
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=RESULTADOS_HTML))

    async with DummySource(min_delay=0.25) as source:
        inicio = time.monotonic()
        await source.search(SearchFilters(keywords="python"))
        await source.search(SearchFilters(keywords="java"))
        transcurrido = time.monotonic() - inicio

    assert transcurrido >= 0.25


async def test_el_rate_limit_se_comparte_entre_fuentes_del_mismo_dominio():
    # Dos adaptadores distintos contra el mismo host no deben poder duplicar
    # el ritmo de peticiones: la cola es por dominio, no por instancia.
    inicio = time.monotonic()
    await asyncio.gather(
        RateLimiter.acquire("example.test", 0.25),
        RateLimiter.acquire("example.test", 0.25),
    )
    assert time.monotonic() - inicio >= 0.25


async def test_hosts_distintos_no_se_bloquean_entre_si():
    inicio = time.monotonic()
    await asyncio.gather(
        RateLimiter.acquire("uno.test", 0.3),
        RateLimiter.acquire("dos.test", 0.3),
    )
    assert time.monotonic() - inicio < 0.3


# --------------------------------------------------------------------------- #
# Resiliencia: nada escapa sin ser SourceError
# --------------------------------------------------------------------------- #


@respx.mock
async def test_un_error_http_se_convierte_en_source_error():
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ROBOTS_PERMISIVO))
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))

    async with DummySource(min_delay=0) as source:
        with pytest.raises(SourceError, match="HTTP 503") as exc_info:
            await source.search(SearchFilters(keywords="python"))

    assert exc_info.value.source == "dummy"
    assert isinstance(exc_info.value.cause, httpx.HTTPStatusError)


@respx.mock
async def test_un_fallo_de_red_se_convierte_en_source_error():
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ROBOTS_PERMISIVO))
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))

    async with DummySource(min_delay=0) as source:
        with pytest.raises(SourceError, match="fallo de red") as exc_info:
            await source.search(SearchFilters(keywords="python"))

    assert isinstance(exc_info.value.cause, httpx.ConnectTimeout)


async def test_un_cliente_inyectado_no_lo_cierra_la_fuente():
    # El aggregator podrá compartir un cliente entre fuentes: cerrarlo es suyo.
    async with httpx.AsyncClient() as client:
        source = DummySource(client=client)
        await source.aclose()
        assert not client.is_closed
