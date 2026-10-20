"""Tests del servidor MCP.

Se llama a los tools por el mismo camino que usa Claude (``call_tool``), no a
las funciones por detrás, para que el test cubra también el esquema de
parámetros que genera el SDK.

Lo que se verifica además del camino feliz: que la regla de producto viaja en
las respuestas (ninguna puede olvidar el aviso de no elegibilidad ni la cita que
exige la licencia del BOE) y que un índice vacío se distingue de "no hay nada
que encaje".
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from convocatorias_mcp.config import Settings
from convocatorias_mcp.db import open_index
from convocatorias_mcp.index import guardar_convocatorias
from convocatorias_mcp.models import AVISO_NO_ELEGIBILIDAD, Seccion
from convocatorias_mcp.server import construir_servidor

FECHA = date(2026, 10, 17)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        watchlists_path=tmp_path / "watchlists.yaml",
        index_path=tmp_path / "indice.sqlite3",
    )


@pytest.fixture
def servidor(settings: Settings):
    return construir_servidor(settings)


@pytest.fixture
def con_datos(settings: Settings, convocatoria_factory):
    """Índice con tres convocatorias de dos secciones distintas."""
    conn = open_index(settings.index_path)
    try:
        guardar_convocatorias(
            conn,
            [
                convocatoria_factory(
                    identificador="BOE-A-2026-1",
                    titulo="Beca de Ingeniería Informática",
                    seccion=Seccion.III,
                ),
                convocatoria_factory(
                    identificador="BOE-A-2026-2",
                    titulo="Convocatoria de plazas de programador",
                    seccion=Seccion.II_B,
                ),
                convocatoria_factory(
                    identificador="BOE-A-2026-3",
                    titulo="Subvención para ganadería extensiva",
                    seccion=Seccion.III,
                ),
            ],
        )
    finally:
        conn.close()
    return settings


@pytest.fixture
def descarga_mockeada(documento_boe):
    """Simula el XML del documento para las llamadas que descargan texto."""
    with respx.mock:
        yield respx.get(url__startswith="https://www.boe.es/diario_boe/xml.php").mock(
            return_value=httpx.Response(
                200, text=documento_boe, headers={"Content-Type": "application/xml"}
            )
        )


async def llamar(servidor, nombre: str, argumentos: dict | None = None) -> dict:
    resultado = await servidor.call_tool(nombre, argumentos or {})
    return resultado.structured_content or {}


# --------------------------------------------------------------------------- #
# Los tools
# --------------------------------------------------------------------------- #


def test_el_servidor_se_identifica(servidor):
    assert servidor.name == "convocatorias"
    assert AVISO_NO_ELEGIBILIDAD in (servidor.instructions or "")


async def test_expone_los_tools_esperados(servidor):
    nombres = {tool.name for tool in await servidor.list_tools()}
    assert nombres == {
        "search_convocatorias",
        "get_convocatoria",
        "source_health",
        "explain_match",
        "deadlines_soon",
        "mark_solicitada",
        "discard",
        "list_solicitudes",
    }


async def test_solo_se_registran_tools_que_funcionan(servidor):
    # run_digest depende del digest, que todavia no existe: un tool declarado
    # que no funciona es peor que uno que no existe, porque el modelo lo
    # intenta y se lleva un error.
    nombres = {tool.name for tool in await servidor.list_tools()}
    assert "run_digest" not in nombres


async def test_los_tools_estan_descritos_para_un_modelo(servidor):
    for tool in await servidor.list_tools():
        descripcion = (tool.description or "").strip()
        # Una descripcion vaga se traduce en llamadas inutiles: se exige que
        # expliquen cuando usarlos, no solo que existen.
        assert len(descripcion) > 120, tool.name
        # Tienen que decir CUANDO usarlos, no solo que existen.
        guias = ("Úsalo", "Llámalo", "Pídelo", "buena consulta")
        assert any(guia in descripcion for guia in guias), tool.name


ESCRIBEN = {"mark_solicitada", "discard"}


async def test_cada_tool_declara_si_escribe(servidor):
    for tool in await servidor.list_tools():
        assert tool.annotations is not None, tool.name
        escribe = tool.name in ESCRIBEN
        assert tool.annotations.read_only_hint is not escribe, tool.name
        if escribe:
            # Anotan un estado del usuario, no destruyen nada.
            assert tool.annotations.destructive_hint is False, tool.name


async def test_search_declara_sus_parametros(servidor):
    tools = await servidor.list_tools()
    tool = next(t for t in tools if t.name == "search_convocatorias")
    propiedades = set((tool.input_schema.get("properties") or {}).keys())
    assert {"keywords", "seccion", "desde", "hasta", "limite"} <= propiedades
    # Ninguno es obligatorio: "que ha salido hoy" es una pregunta valida.
    assert not tool.input_schema.get("required")


async def test_search_no_expone_ambito(servidor):
    """El BOE no publica el ambito: filtrar por el devolveria cero siempre.

    Un parametro que no puede funcionar es peor que uno que no existe, porque
    el modelo lo intentaria. Mismo criterio que con run_digest.
    """
    tools = await servidor.list_tools()
    tool = next(t for t in tools if t.name == "search_convocatorias")
    propiedades = set((tool.input_schema.get("properties") or {}).keys())
    assert "ambito" not in propiedades
    assert "ambito" not in (tool.description or "")


# --------------------------------------------------------------------------- #
# search_convocatorias
# --------------------------------------------------------------------------- #


async def test_search_encuentra_por_palabra_clave(con_datos, servidor):
    datos = await llamar(servidor, "search_convocatorias", {"keywords": "informatica"})

    assert datos["total"] == 1
    assert datos["resultados"][0]["identificador_oficial"] == "BOE-A-2026-1"


async def test_search_filtra_por_seccion_escrita_a_mano(con_datos, servidor):
    datos = await llamar(servidor, "search_convocatorias", {"seccion": "II.B"})

    assert datos["total"] == 1
    assert datos["resultados"][0]["seccion"] == "II.B"


async def test_search_acepta_el_codigo_interno_de_seccion(con_datos, servidor):
    # Un modelo puede pasar "2B" si lo ha visto en una respuesta anterior.
    datos = await llamar(servidor, "search_convocatorias", {"seccion": "2B"})
    assert datos["total"] == 1


async def test_search_sin_filtros_devuelve_lo_mas_reciente(con_datos, servidor):
    datos = await llamar(servidor, "search_convocatorias", {})
    assert datos["total"] == 3
    assert datos["indice_cubre_hasta"] == FECHA.isoformat()


async def test_search_respeta_el_limite(con_datos, servidor):
    datos = await llamar(servidor, "search_convocatorias", {"limite": 2})
    assert datos["total"] == 2


async def test_search_devuelve_el_mismo_orden_en_dos_llamadas(con_datos, servidor):
    primera = await llamar(servidor, "search_convocatorias", {})
    segunda = await llamar(servidor, "search_convocatorias", {})

    assert [r["id"] for r in primera["resultados"]] == [r["id"] for r in segunda["resultados"]]


async def test_search_avisa_de_que_el_indice_esta_vacio(servidor):
    # Sin esto el modelo diria "no hay convocatorias" cuando lo que pasa es que
    # falta ingerir.
    datos = await llamar(servidor, "search_convocatorias", {"keywords": "becas"})

    assert datos["total"] == 0
    assert "ingest" in datos["indice_vacio"]


async def test_search_no_devuelve_el_texto_completo(con_datos, servidor):
    datos = await llamar(servidor, "search_convocatorias", {})
    assert "texto_completo" not in datos["resultados"][0]


async def test_search_explica_un_plazo_desconocido(con_datos, servidor):
    datos = await llamar(servidor, "search_convocatorias", {})
    assert datos["resultados"][0]["plazo_fin"] is None
    assert "desconocido" in datos["resultados"][0]["plazo"]


# --------------------------------------------------------------------------- #
# get_convocatoria
# --------------------------------------------------------------------------- #


async def test_get_devuelve_una_convocatoria(con_datos, servidor, descarga_mockeada):
    datos = await llamar(servidor, "get_convocatoria", {"id": "boe:BOE-A-2026-1"})

    assert datos["encontrada"] is True
    assert datos["titulo"] == "Beca de Ingeniería Informática"
    assert datos["url_oficial"].startswith("https://www.boe.es/")


async def test_get_descarga_el_texto_bajo_demanda(con_datos, servidor, descarga_mockeada):
    # Es el nivel 2 del matching: sin esto el modelo resumiria requisitos que
    # no ha leido.
    datos = await llamar(servidor, "get_convocatoria", {"id": "boe:BOE-A-2026-1"})

    assert descarga_mockeada.called
    assert "De acuerdo con lo dispuesto" in datos["texto_completo"]
    assert "nota_texto" not in datos


async def test_get_cachea_el_texto_y_no_lo_vuelve_a_pedir(con_datos, servidor, descarga_mockeada):
    await llamar(servidor, "get_convocatoria", {"id": "boe:BOE-A-2026-1"})
    primera = descarga_mockeada.call_count

    datos = await llamar(servidor, "get_convocatoria", {"id": "boe:BOE-A-2026-1"})

    assert descarga_mockeada.call_count == primera == 1
    assert "De acuerdo con lo dispuesto" in datos["texto_completo"]


async def test_si_la_descarga_falla_el_tool_sigue_sirviendo(con_datos, servidor):
    # Un fallo de red no debe romper la llamada: la convocatoria y su enlace
    # siguen valiendo, y la nota avisa de que el texto no se ha leido.
    with respx.mock:
        respx.get(url__startswith="https://www.boe.es/diario_boe/xml.php").mock(
            return_value=httpx.Response(503)
        )
        datos = await llamar(servidor, "get_convocatoria", {"id": "boe:BOE-A-2026-1"})

    assert datos["encontrada"] is True
    assert datos["texto_completo"] is None
    assert "no deduzcas sus requisitos" in datos["nota_texto"]
    assert "boe.es" in datos["nota_texto"]


async def test_una_fuente_que_no_descarga_textos_lo_dice(settings, servidor, convocatoria_factory):
    conn = open_index(settings.index_path)
    try:
        guardar_convocatorias(conn, [convocatoria_factory(fuente="fixture")])
    finally:
        conn.close()

    datos = await llamar(servidor, "get_convocatoria", {"id": "fixture:BOE-A-2026-21635"})

    assert datos["texto_completo"] is None
    assert "no descarga textos completos" in datos["nota_texto"]


async def test_get_no_descarga_si_el_texto_ya_esta_en_el_indice(
    settings, servidor, convocatoria_factory
):
    conn = open_index(settings.index_path)
    try:
        guardar_convocatorias(
            conn, [convocatoria_factory(texto_completo="De acuerdo con lo dispuesto...")]
        )
    finally:
        conn.close()

    datos = await llamar(servidor, "get_convocatoria", {"id": "boe:BOE-A-2026-21635"})

    assert datos["texto_completo"] == "De acuerdo con lo dispuesto..."
    assert "nota_texto" not in datos


async def test_get_con_un_id_que_no_existe_lo_explica(con_datos, servidor):
    datos = await llamar(servidor, "get_convocatoria", {"id": "boe:BOE-A-1999-1"})

    assert datos["encontrada"] is False
    assert "search_convocatorias" in datos["error"]


# --------------------------------------------------------------------------- #
# source_health
# --------------------------------------------------------------------------- #


async def test_source_health_informa_del_indice(con_datos, servidor):
    datos = await llamar(servidor, "source_health")

    assert datos["convocatorias_indexadas"] == 3
    assert datos["ultima_publicacion_indexada"] == FECHA.isoformat()
    # No se ha registrado ninguna ingesta: no es lo mismo que tener datos.
    assert datos["ultima_fecha_comprobada"] is None
    assert datos["huecos_totales"] == 0
    assert datos["fuentes"][0]["licencia"].startswith("https://www.boe.es/")


async def test_source_health_funciona_con_el_indice_vacio(servidor):
    datos = await llamar(servidor, "source_health")

    assert datos["convocatorias_indexadas"] == 0
    assert datos["dias_registrados"] == 0
    assert "ingest" in datos["resumen"]


# --------------------------------------------------------------------------- #
# Reglas de producto que viajan en toda respuesta
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "argumentos"),
    [
        ("search_convocatorias", {}),
        ("get_convocatoria", {"id": "boe:BOE-A-2026-1"}),
    ],
)
async def test_toda_respuesta_lleva_el_aviso_de_no_elegibilidad(
    con_datos, servidor, descarga_mockeada, tool, argumentos
):
    datos = await llamar(servidor, tool, argumentos)
    assert datos["aviso"] == AVISO_NO_ELEGIBILIDAD


@pytest.mark.parametrize(
    ("tool", "argumentos"),
    [
        ("search_convocatorias", {}),
        ("get_convocatoria", {"id": "boe:BOE-A-2026-1"}),
        ("source_health", {}),
    ],
)
async def test_toda_respuesta_lleva_la_cita_que_exige_la_licencia(
    con_datos, servidor, descarga_mockeada, tool, argumentos
):
    datos = await llamar(servidor, tool, argumentos)
    assert "Boletín Oficial del Estado" in datos["cita_obligatoria"]
    assert "boe.es" in datos["cita_obligatoria"]
