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

import pytest

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
    assert nombres == {"search_convocatorias", "get_convocatoria", "source_health"}


async def test_los_tools_estan_descritos_para_un_modelo(servidor):
    for tool in await servidor.list_tools():
        descripcion = (tool.description or "").strip()
        # Una descripcion vaga se traduce en llamadas inutiles: se exige que
        # expliquen cuando usarlos, no solo que existen.
        assert len(descripcion) > 120, tool.name
        assert "Úsalo" in descripcion or "Llámalo" in descripcion, tool.name


async def test_los_tools_se_declaran_de_solo_lectura(servidor):
    for tool in await servidor.list_tools():
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name


async def test_search_declara_sus_parametros(servidor):
    tools = await servidor.list_tools()
    tool = next(t for t in tools if t.name == "search_convocatorias")
    propiedades = set((tool.input_schema.get("properties") or {}).keys())
    assert {"keywords", "seccion", "desde", "hasta", "ambito", "limite"} <= propiedades
    # Ninguno es obligatorio: "que ha salido hoy" es una pregunta valida.
    assert not tool.input_schema.get("required")


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


async def test_get_devuelve_una_convocatoria(con_datos, servidor):
    datos = await llamar(servidor, "get_convocatoria", {"id": "boe:BOE-A-2026-1"})

    assert datos["encontrada"] is True
    assert datos["titulo"] == "Beca de Ingeniería Informática"
    assert datos["url_oficial"].startswith("https://www.boe.es/")


async def test_get_sin_texto_descargado_manda_al_texto_oficial(con_datos, servidor):
    datos = await llamar(servidor, "get_convocatoria", {"id": "boe:BOE-A-2026-1"})

    assert datos["texto_completo"] is None
    assert "no los deduzcas del título" in datos["nota_texto"]
    assert "boe.es" in datos["nota_texto"]


async def test_get_devuelve_el_texto_cuando_esta_descargado(
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
    con_datos, servidor, tool, argumentos
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
    con_datos, servidor, tool, argumentos
):
    datos = await llamar(servidor, tool, argumentos)
    assert "Boletín Oficial del Estado" in datos["cita_obligatoria"]
    assert "boe.es" in datos["cita_obligatoria"]
