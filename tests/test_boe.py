"""Tests de la fuente real del BOE.

Las respuestas se simulan con ``respx`` usando los **ficheros reales** que se
capturaron de la API: el sumario del 17/10/2026 y el XML de un documento. Así
el test cubre el mapeo de verdad y no una idealización de lo que devuelve el
BOE.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx
from bs4 import BeautifulSoup

from convocatorias_mcp.models import Seccion
from convocatorias_mcp.sources.base import Consentimiento, SinPublicacion, SourceError
from convocatorias_mcp.sources.boe import BoeSource, extraer_texto

FECHA = date(2026, 10, 17)
URL_SUMARIO = "https://www.boe.es/datosabiertos/api/boe/sumario/20261017"
URL_ROBOTS = "https://www.boe.es/robots.txt"
URL_XML = "https://www.boe.es/diario_boe/xml.php?id=BOE-A-2026-21635"

# Lo que devuelve la API un domingo: 404 y cuerpo XML, aunque pidas JSON.
RESPUESTA_404 = (
    '<?xml version="1.0" encoding="utf-8"?>\n<response>\n  <status>\n'
    "    <code>404</code>\n    <text>La información solicitada no existe</text>\n"
    "  </status>\n  <data/>\n</response>\n"
)


# --------------------------------------------------------------------------- #
# El cortafuegos de la suite
# --------------------------------------------------------------------------- #


def test_el_cortafuegos_de_red_funciona():
    # Un cortafuegos que no salta es peor que no tenerlo: esto lo verifica.
    with pytest.raises(Exception, match="intentado salir a la red"):
        httpx.get("https://www.boe.es/datosabiertos/api/boe/sumario/20261017")


# --------------------------------------------------------------------------- #
# Metadatos de la fuente
# --------------------------------------------------------------------------- #


def test_la_fuente_declara_su_consentimiento_y_su_licencia():
    assert BoeSource.name == "boe"
    assert BoeSource.method == "api"
    # El permiso viene de la licencia de reutilización, no de robots.txt.
    assert BoeSource.consentimiento is Consentimiento.DATOS_ABIERTOS
    assert BoeSource.licencia_url is not None
    assert "aviso_legal" in BoeSource.licencia_url
    assert BoeSource.cita_requerida == (
        "Fuente de los datos: Agencia Estatal Boletín Oficial del Estado"
    )


def test_construye_bien_la_url_del_sumario():
    assert BoeSource().url_sumario(FECHA) == URL_SUMARIO


# --------------------------------------------------------------------------- #
# fetch_dia
# --------------------------------------------------------------------------- #


@respx.mock
async def test_fetch_dia_mapea_el_sumario_real(sumario_boe):
    respx.get(URL_SUMARIO).mock(return_value=httpx.Response(200, json=sumario_boe))

    async with BoeSource(min_delay=0) as fuente:
        convocatorias = await fuente.fetch_dia(FECHA)

    assert len(convocatorias) == 270
    assert all(convocatoria.fuente == "boe" for convocatoria in convocatorias)
    assert all(convocatoria.fecha_publicacion == FECHA for convocatoria in convocatorias)
    # Los ids llevan el prefijo de la fuente real, no el de la de desarrollo.
    assert convocatorias[0].id.startswith("boe:")


@respx.mock
async def test_fetch_dia_pide_json_con_el_user_agent_del_proyecto(sumario_boe):
    ruta = respx.get(URL_SUMARIO).mock(return_value=httpx.Response(200, json=sumario_boe))

    async with BoeSource(min_delay=0) as fuente:
        await fuente.fetch_dia(FECHA)

    peticion = ruta.calls.last.request
    assert peticion.headers["accept"] == "application/json"
    assert peticion.headers["user-agent"].startswith("convocatorias-mcp/")


@respx.mock
async def test_un_404_es_dia_sin_publicacion_no_un_error():
    respx.get(URL_SUMARIO).mock(
        return_value=httpx.Response(
            404, text=RESPUESTA_404, headers={"Content-Type": "application/xml"}
        )
    )

    async with BoeSource(min_delay=0) as fuente:
        with pytest.raises(SinPublicacion) as exc_info:
            await fuente.fetch_dia(FECHA)

    assert exc_info.value.fecha == FECHA
    assert not isinstance(exc_info.value, SourceError)


@respx.mock
async def test_un_5xx_si_es_un_error_con_su_codigo():
    respx.get(URL_SUMARIO).mock(return_value=httpx.Response(503))

    async with BoeSource(min_delay=0) as fuente:
        with pytest.raises(SourceError) as exc_info:
            await fuente.fetch_dia(FECHA)

    assert exc_info.value.status_code == 503


@respx.mock
async def test_una_respuesta_que_no_es_json_se_explica():
    respx.get(URL_SUMARIO).mock(return_value=httpx.Response(200, text="<html>vaya</html>"))

    async with BoeSource(min_delay=0) as fuente:
        with pytest.raises(SourceError, match="no es JSON"):
            await fuente.fetch_dia(FECHA)


@respx.mock
async def test_un_json_sin_la_forma_de_sumario_se_explica():
    respx.get(URL_SUMARIO).mock(return_value=httpx.Response(200, json={"otra": "cosa"}))

    async with BoeSource(min_delay=0) as fuente:
        with pytest.raises(SourceError, match="no tiene la forma esperada"):
            await fuente.fetch_dia(FECHA)


@respx.mock
async def test_un_sumario_sin_convocatorias_se_trata_como_estructura_cambiada():
    # El BOE no publica sumarios vacios: si no sale nada, es que ha cambiado la
    # estructura, y guardar el dia en blanco lo esconderia para siempre.
    vacio = {"data": {"sumario": {"metadatos": {"fecha_publicacion": "20261017"}, "diario": []}}}
    respx.get(URL_SUMARIO).mock(return_value=httpx.Response(200, json=vacio))

    async with BoeSource(min_delay=0) as fuente:
        with pytest.raises(SourceError, match="ha cambiado su estructura"):
            await fuente.fetch_dia(FECHA)


@respx.mock
async def test_no_consulta_robots_txt(sumario_boe):
    # Es datos abiertos con licencia: el permiso no viene de robots.txt. Y su
    # robots.txt pesa 494 KB, asi que pedirlo en cada arranque seria absurdo.
    ruta_robots = respx.get(URL_ROBOTS).mock(return_value=httpx.Response(200, text="User-agent: *"))
    respx.get(URL_SUMARIO).mock(return_value=httpx.Response(200, json=sumario_boe))

    async with BoeSource(min_delay=0) as fuente:
        await fuente.fetch_dia(FECHA)

    assert not ruta_robots.called


# --------------------------------------------------------------------------- #
# fetch_texto
# --------------------------------------------------------------------------- #


@respx.mock
async def test_fetch_texto_descarga_y_extrae_el_texto(convocatoria_factory, documento_boe):
    respx.get(URL_XML).mock(
        return_value=httpx.Response(
            200, text=documento_boe, headers={"Content-Type": "application/xml"}
        )
    )
    convocatoria = convocatoria_factory(fuente="boe", url_xml=URL_XML)

    async with BoeSource(min_delay=0) as fuente:
        con_texto = await fuente.fetch_texto(convocatoria)

    assert con_texto.texto_descargado
    assert con_texto.texto_descargado_at is not None
    assert "De acuerdo con lo dispuesto" in (con_texto.texto_completo or "")
    # Y el resto de la convocatoria no se toca.
    assert con_texto.identificador == convocatoria.identificador
    assert con_texto.id == convocatoria.id


@respx.mock
async def test_fetch_texto_no_vuelve_a_descargar_lo_que_ya_tiene(convocatoria_factory):
    ruta = respx.get(URL_XML).mock(return_value=httpx.Response(200, text="<documento/>"))
    convocatoria = convocatoria_factory(
        fuente="boe", url_xml=URL_XML, texto_completo="ya descargado"
    )

    async with BoeSource(min_delay=0) as fuente:
        resultado = await fuente.fetch_texto(convocatoria)

    assert resultado is convocatoria
    assert not ruta.called


@respx.mock
async def test_un_xml_sin_texto_da_source_error(convocatoria_factory):
    respx.get(URL_XML).mock(
        return_value=httpx.Response(200, text="<documento><metadatos/></documento>")
    )
    convocatoria = convocatoria_factory(fuente="boe", url_xml=URL_XML)

    async with BoeSource(min_delay=0) as fuente:
        with pytest.raises(SourceError, match="no trae texto legible"):
            await fuente.fetch_texto(convocatoria)


# --------------------------------------------------------------------------- #
# Extraccion del texto
# --------------------------------------------------------------------------- #


def test_extraer_texto_conserva_los_parrafos(documento_boe):
    texto = extraer_texto(BeautifulSoup(documento_boe, "lxml-xml"))

    assert texto is not None
    # Separado en parrafos, porque esto lo va a leer un modelo.
    assert "\n\n" in texto
    assert "Bases" in texto
    assert len(texto) > 1000


def test_extraer_texto_normaliza_los_espacios_duros(documento_boe):
    texto = extraer_texto(BeautifulSoup(documento_boe, "lxml-xml")) or ""
    # El BOE usa U+00A0 a mansalva y ensucia cualquier busqueda posterior.
    assert chr(160) not in texto


def test_extraer_texto_aplana_las_tablas():
    xml = """
    <documento><texto>
      <p>Antes</p>
      <table><tr><th>Plaza</th><th>Número</th></tr><tr><td>Analista</td><td>3</td></tr></table>
      <p>Después</p>
    </texto></documento>
    """
    texto = extraer_texto(BeautifulSoup(xml, "lxml-xml")) or ""

    assert "Plaza | Número" in texto
    assert "Analista | 3" in texto
    assert "Antes" in texto and "Después" in texto


def test_extraer_texto_sin_parrafos_devuelve_el_texto_plano():
    xml = "<documento><texto>Un texto suelto sin etiquetas</texto></documento>"
    assert extraer_texto(BeautifulSoup(xml, "lxml-xml")) == "Un texto suelto sin etiquetas"


def test_extraer_texto_sin_nodo_texto_devuelve_none():
    xml = "<documento><metadatos><titulo>Algo</titulo></metadatos></documento>"
    assert extraer_texto(BeautifulSoup(xml, "lxml-xml")) is None


# --------------------------------------------------------------------------- #
# Secciones
# --------------------------------------------------------------------------- #


@respx.mock
async def test_todas_las_secciones_del_dia_se_reconocen(sumario_boe):
    respx.get(URL_SUMARIO).mock(return_value=httpx.Response(200, json=sumario_boe))

    async with BoeSource(min_delay=0) as fuente:
        convocatorias = await fuente.fetch_dia(FECHA)

    secciones = {convocatoria.seccion for convocatoria in convocatorias}
    assert Seccion.DESCONOCIDA not in secciones
    assert {Seccion.II_B, Seccion.III, Seccion.V_B} <= secciones
