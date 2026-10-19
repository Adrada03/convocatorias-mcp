"""Tests del contrato de fuente y del acceso educado.

Lo que se verifica con tests, y no solo con documentación: que un día sin
publicación se distingue de un error, que el User-Agent se identifica, que el
retardo entre peticiones existe, que ningún fallo de red escapa sin convertirse
en ``SourceError``, y que ``robots.txt`` se comprueba **solo** en las fuentes
cuyo consentimiento se apoya en él.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date

import httpx
import pytest
import respx

from convocatorias_mcp.models import Convocatoria, Seccion
from convocatorias_mcp.sources.base import (
    DEFAULT_MIN_DELAY_SECONDS,
    BoletinSource,
    Consentimiento,
    PoliteHttpSource,
    RateLimiter,
    SinPublicacion,
    SourceError,
    user_agent,
)

ROBOTS_URL = "https://boletin.test/robots.txt"
SUMARIO_URL = "https://boletin.test/api/sumario"
ROBOTS_PERMISIVO = "User-agent: *\nDisallow: /privado\n"

SUMARIO_JSON = {
    "status": {"code": 200},
    "data": {
        "sumario": {
            "metadatos": {"publicacion": "TEST", "fecha_publicacion": "20261017"},
            "diario": [
                {
                    "numero": "250",
                    "seccion": [
                        {
                            "codigo": "2B",
                            "nombre": "II. Autoridades y personal. - B. Oposiciones y concursos",
                            "departamento": [
                                {
                                    "nombre": "MINISTERIO DE DEFENSA",
                                    "epigrafe": [
                                        {
                                            "nombre": "Procedimientos de libre designación",
                                            "item": [
                                                {
                                                    "identificador": "BOE-A-2026-21635",
                                                    "titulo": "Resolución por la que se convoca",
                                                    "url_html": "https://boletin.test/txt/21635",
                                                    "url_xml": "https://boletin.test/xml/21635",
                                                    "url_pdf": {
                                                        "texto": "https://boletin.test/pdf/21635",
                                                        "szBytes": "279865",
                                                    },
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    },
}


class FuenteDeDatosAbiertos(PoliteHttpSource):
    """Fuente mínima de datos abiertos: lo justo que escribe quien añade un boletín."""

    name = "test-abierto"
    display_name = "Boletín de prueba"
    base_url = "https://boletin.test"
    method = "api"
    consentimiento = Consentimiento.DATOS_ABIERTOS
    licencia_url = "https://boletin.test/licencia"

    async def fetch_dia(self, fecha: date) -> list[Convocatoria]:
        try:
            datos = await self.fetch_json(f"{SUMARIO_URL}/{fecha.strftime('%Y%m%d')}")
        except SourceError as exc:
            # Un 404 es ausencia de publicación, no un fallo de la fuente.
            if exc.status_code == 404:
                raise SinPublicacion(self.name, fecha) from exc
            raise

        convocatorias = []
        for diario in datos["data"]["sumario"]["diario"]:
            for seccion in diario["seccion"]:
                for departamento in seccion["departamento"]:
                    for epigrafe in departamento["epigrafe"]:
                        for item in epigrafe["item"]:
                            convocatorias.append(
                                Convocatoria(
                                    fuente=self.name,
                                    identificador=item["identificador"],
                                    titulo=item["titulo"],
                                    seccion=Seccion.from_api_code(seccion["codigo"]),
                                    seccion_nombre=seccion["nombre"],
                                    departamento=departamento["nombre"],
                                    epigrafe=epigrafe["nombre"],
                                    fecha_publicacion=fecha,
                                    url_html=item["url_html"],
                                    url_xml=item["url_xml"],
                                    url_pdf=(item.get("url_pdf") or {}).get("texto"),
                                )
                            )
        return convocatorias


class FuenteConRobots(FuenteDeDatosAbiertos):
    """La misma fuente, pero apoyándose en robots.txt para el permiso."""

    name = "test-robots"
    method = "scraping"
    consentimiento = Consentimiento.ROBOTS
    licencia_url = None


# --------------------------------------------------------------------------- #
# El contrato
# --------------------------------------------------------------------------- #


def test_no_se_puede_instanciar_una_fuente_sin_fetch_dia():
    class Incompleta(BoletinSource):
        name = "incompleta"
        display_name = "Incompleta"
        base_url = "https://boletin.test"
        method = "api"
        consentimiento = Consentimiento.DATOS_ABIERTOS

    with pytest.raises(TypeError):
        Incompleta()  # type: ignore[abstract]


async def test_fetch_texto_por_defecto_devuelve_la_convocatoria_tal_cual(convocatoria_factory):
    fuente = FuenteDeDatosAbiertos()
    original = convocatoria_factory()
    assert await fuente.fetch_texto(original) is original


def test_una_fuente_declara_su_licencia_y_su_consentimiento():
    fuente = FuenteDeDatosAbiertos()
    assert fuente.consentimiento is Consentimiento.DATOS_ABIERTOS
    assert fuente.licencia_url == "https://boletin.test/licencia"
    assert fuente.is_available()


def test_source_error_identifica_la_fuente_y_conserva_la_causa():
    causa = httpx.ConnectTimeout("timeout")
    error = SourceError("boe", "fallo de red", cause=causa, status_code=503)
    assert str(error) == "[boe] fallo de red"
    assert error.source == "boe"
    assert error.status_code == 503
    assert error.__cause__ is causa


def test_sin_publicacion_no_es_un_source_error():
    # Tiene que ser imposible confundirlos: uno degrada la fuente y el otro no.
    sin_publicacion = SinPublicacion("boe", date(2026, 10, 18))
    assert not isinstance(sin_publicacion, SourceError)
    assert sin_publicacion.fecha == date(2026, 10, 18)
    assert "2026-10-18" in str(sin_publicacion)


# --------------------------------------------------------------------------- #
# User-Agent
# --------------------------------------------------------------------------- #


def test_el_user_agent_es_honesto_y_contactable():
    agent = user_agent()
    assert agent.startswith("convocatorias-mcp/")
    assert "github.com/Adrada03/convocatorias-mcp" in agent
    for impostor in ("Mozilla", "Chrome", "Safari", "AppleWebKit"):
        assert impostor not in agent


@respx.mock
async def test_las_peticiones_envian_el_user_agent_del_proyecto():
    ruta = respx.get(f"{SUMARIO_URL}/20261017").mock(
        return_value=httpx.Response(200, json=SUMARIO_JSON)
    )

    async with FuenteDeDatosAbiertos(min_delay=0) as fuente:
        await fuente.fetch_dia(date(2026, 10, 17))

    assert ruta.calls.last.request.headers["user-agent"] == user_agent()
    assert ruta.calls.last.request.headers["accept"] == "application/json"


# --------------------------------------------------------------------------- #
# Camino feliz
# --------------------------------------------------------------------------- #


@respx.mock
async def test_fetch_dia_mapea_el_sumario_al_modelo():
    respx.get(f"{SUMARIO_URL}/20261017").mock(return_value=httpx.Response(200, json=SUMARIO_JSON))

    async with FuenteDeDatosAbiertos(min_delay=0) as fuente:
        convocatorias = await fuente.fetch_dia(date(2026, 10, 17))

    assert len(convocatorias) == 1
    convocatoria = convocatorias[0]
    assert convocatoria.identificador == "BOE-A-2026-21635"
    assert convocatoria.seccion is Seccion.II_B
    assert convocatoria.departamento == "MINISTERIO DE DEFENSA"
    # url_pdf viene dentro de un objeto, no como cadena.
    assert convocatoria.url_pdf == "https://boletin.test/pdf/21635"
    assert convocatoria.plazo_fin is None


# --------------------------------------------------------------------------- #
# Dia sin publicacion
# --------------------------------------------------------------------------- #


@respx.mock
async def test_un_404_se_traduce_a_sin_publicacion():
    # El BOE responde 404 los domingos, y con cuerpo XML aunque pidas JSON.
    respx.get(f"{SUMARIO_URL}/20261018").mock(
        return_value=httpx.Response(
            404,
            text='<?xml version="1.0"?><response><status><code>404</code></status></response>',
            headers={"Content-Type": "application/xml"},
        )
    )

    async with FuenteDeDatosAbiertos(min_delay=0) as fuente:
        with pytest.raises(SinPublicacion) as exc_info:
            await fuente.fetch_dia(date(2026, 10, 18))

    assert exc_info.value.fecha == date(2026, 10, 18)


@respx.mock
async def test_el_codigo_http_viaja_en_el_source_error():
    # Es lo que permite a la fuente distinguir el 404 de cualquier otro fallo.
    respx.get(f"{SUMARIO_URL}/20261017").mock(return_value=httpx.Response(500))

    async with FuenteDeDatosAbiertos(min_delay=0) as fuente:
        with pytest.raises(SourceError) as exc_info:
            await fuente.fetch_dia(date(2026, 10, 17))

    assert exc_info.value.status_code == 500


# --------------------------------------------------------------------------- #
# robots.txt solo donde toca
# --------------------------------------------------------------------------- #


@respx.mock
async def test_una_fuente_de_datos_abiertos_no_consulta_robots():
    # api.adzuna.com tiene "Disallow: /" y api.ashbyhq.com responde 401 a su
    # robots.txt: comprobarlo en una API publicada para terceros dejaria el
    # proyecto sin fuentes cumpliendo la letra de un documento que no va
    # dirigido a nosotros.
    ruta_robots = respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    respx.get(f"{SUMARIO_URL}/20261017").mock(return_value=httpx.Response(200, json=SUMARIO_JSON))

    async with FuenteDeDatosAbiertos(min_delay=0) as fuente:
        convocatorias = await fuente.fetch_dia(date(2026, 10, 17))

    assert len(convocatorias) == 1
    assert not ruta_robots.called


@respx.mock
async def test_una_fuente_de_scraping_si_obedece_robots():
    respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /api\n")
    )
    ruta_sumario = respx.get(f"{SUMARIO_URL}/20261017").mock(
        return_value=httpx.Response(200, json=SUMARIO_JSON)
    )

    async with FuenteConRobots(min_delay=0) as fuente:
        with pytest.raises(SourceError, match=r"robots\.txt"):
            await fuente.fetch_dia(date(2026, 10, 17))

    assert not ruta_sumario.called


@respx.mock
async def test_el_consentimiento_se_puede_forzar_en_los_tests():
    ruta_robots = respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text=ROBOTS_PERMISIVO)
    )
    respx.get(f"{SUMARIO_URL}/20261017").mock(return_value=httpx.Response(200, json=SUMARIO_JSON))

    async with FuenteDeDatosAbiertos(min_delay=0, respect_robots=True) as fuente:
        await fuente.fetch_dia(date(2026, 10, 17))

    assert ruta_robots.called


@respx.mock
async def test_el_crawl_delay_del_sitio_gana_si_es_mayor():
    respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 9\nDisallow:\n")
    )

    fuente = FuenteConRobots()
    try:
        delay = await fuente._robots_crawl_delay(SUMARIO_URL)
    finally:
        await fuente.aclose()

    assert delay == 9.0
    assert max(DEFAULT_MIN_DELAY_SECONDS, delay) == 9.0


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


def test_el_retardo_minimo_por_defecto_es_prudente():
    assert DEFAULT_MIN_DELAY_SECONDS >= 1.5


@respx.mock
async def test_dos_peticiones_al_mismo_host_se_espacian():
    respx.get(url__startswith=SUMARIO_URL).mock(return_value=httpx.Response(200, json=SUMARIO_JSON))

    async with FuenteDeDatosAbiertos(min_delay=0.25) as fuente:
        inicio = time.monotonic()
        await fuente.fetch_dia(date(2026, 10, 16))
        await fuente.fetch_dia(date(2026, 10, 17))
        transcurrido = time.monotonic() - inicio

    assert transcurrido >= 0.25


async def test_el_rate_limit_se_comparte_entre_fuentes_del_mismo_dominio():
    # Un backfill concurrente no debe poder duplicar el ritmo de peticiones.
    inicio = time.monotonic()
    await asyncio.gather(
        RateLimiter.acquire("boletin.test", 0.25),
        RateLimiter.acquire("boletin.test", 0.25),
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
# Resiliencia
# --------------------------------------------------------------------------- #


@respx.mock
async def test_un_fallo_de_red_se_convierte_en_source_error():
    respx.get(f"{SUMARIO_URL}/20261017").mock(side_effect=httpx.ConnectTimeout("timeout"))

    async with FuenteDeDatosAbiertos(min_delay=0) as fuente:
        with pytest.raises(SourceError, match="fallo de red") as exc_info:
            await fuente.fetch_dia(date(2026, 10, 17))

    assert isinstance(exc_info.value.cause, httpx.ConnectTimeout)
    assert exc_info.value.status_code is None


@respx.mock
async def test_una_respuesta_que_no_es_json_se_explica():
    respx.get(f"{SUMARIO_URL}/20261017").mock(
        return_value=httpx.Response(200, text="<html>vaya</html>")
    )

    async with FuenteDeDatosAbiertos(min_delay=0) as fuente:
        with pytest.raises(SourceError, match="no es JSON"):
            await fuente.fetch_dia(date(2026, 10, 17))


async def test_un_cliente_inyectado_no_lo_cierra_la_fuente():
    # La ingesta podra compartir un cliente entre fuentes: cerrarlo es suyo.
    async with httpx.AsyncClient() as client:
        fuente = FuenteDeDatosAbiertos(client=client)
        await fuente.aclose()
        assert not client.is_closed
