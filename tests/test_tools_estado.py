"""Tests de los cinco tools nuevos: explicación, plazos y seguimiento.

Se llaman por el mismo camino que usa Claude (``call_tool``), así que el test
cubre también el esquema de parámetros que genera el SDK.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from convocatorias_mcp.config import Settings
from convocatorias_mcp.db import open_index
from convocatorias_mcp.index import guardar_convocatorias
from convocatorias_mcp.models import PlazoOrigen, Seccion
from convocatorias_mcp.server import construir_servidor

HOY = date(2026, 10, 19)

WATCHLISTS = """
watchlists:
  - nombre: becas-informatica
    keywords: [informatica]
    secciones: [III]
  - nombre: empleo-tic
    keywords: [programador]
    secciones: [II.B]
"""


@pytest.fixture(autouse=True)
def _hoy_fijo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("convocatorias_mcp.server.hoy_boletin", lambda: HOY)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        watchlists_path=tmp_path / "watchlists.yaml",
        index_path=tmp_path / "indice.sqlite3",
        auto_ingest_dias=0,
    )


@pytest.fixture
def con_watchlists(settings: Settings) -> Settings:
    settings.watchlists_path.write_text(WATCHLISTS, encoding="utf-8")
    return settings


@pytest.fixture
def con_datos(settings: Settings, convocatoria_factory) -> Settings:
    conn = open_index(settings.index_path)
    try:
        guardar_convocatorias(
            conn,
            [
                convocatoria_factory(
                    identificador="BOE-A-2026-1",
                    titulo="Becas de informática para titulados",
                    seccion=Seccion.III,
                ),
                convocatoria_factory(
                    identificador="BOE-A-2026-2",
                    titulo="Convocatoria de plazas de programador",
                    seccion=Seccion.II_B,
                    plazo_fin=date(2026, 10, 22),
                    plazo_origen=PlazoOrigen.TEXTO,
                ),
                convocatoria_factory(
                    identificador="BOE-A-2026-3",
                    titulo="Subvenciones a la ganadería",
                    seccion=Seccion.III,
                    plazo_fin=date(2026, 12, 1),
                    plazo_origen=PlazoOrigen.TEXTO,
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
# explain_match
# --------------------------------------------------------------------------- #


async def test_explain_match_desglosa_el_score(con_datos, con_watchlists):
    datos = await llamar(construir_servidor(con_datos), "explain_match", {"id": "boe:BOE-A-2026-1"})

    assert datos["explicado"] is True
    becas = next(w for w in datos["watchlists"] if w["watchlist"] == "becas-informatica")
    assert becas["coincide"] is True
    assert becas["keywords_encontradas"] == ["informatica"]
    dimensiones = {componente["dimension"] for componente in becas["componentes"]}
    assert dimensiones == {"titulo", "seccion", "departamento"}
    assert all(componente["detalle"] for componente in becas["componentes"])


async def test_explain_match_explica_tambien_por_que_no_coincide(con_datos, con_watchlists):
    datos = await llamar(construir_servidor(con_datos), "explain_match", {"id": "boe:BOE-A-2026-1"})

    empleo = next(w for w in datos["watchlists"] if w["watchlist"] == "empleo-tic")
    assert empleo["coincide"] is False
    assert "no está entre las que vigila" in empleo["motivo"]


async def test_explain_match_aclara_que_no_mide_elegibilidad(con_datos, con_watchlists):
    datos = await llamar(construir_servidor(con_datos), "explain_match", {"id": "boe:BOE-A-2026-1"})
    assert "No mide elegibilidad" in datos["nota"]
    assert "aviso" in datos


async def test_explain_match_se_puede_pedir_de_una_sola_watchlist(con_datos, con_watchlists):
    datos = await llamar(
        construir_servidor(con_datos),
        "explain_match",
        {"id": "boe:BOE-A-2026-1", "watchlist": "becas-informatica"},
    )
    assert len(datos["watchlists"]) == 1


async def test_explain_match_con_una_watchlist_inexistente_lista_las_validas(
    con_datos, con_watchlists
):
    datos = await llamar(
        construir_servidor(con_datos),
        "explain_match",
        {"id": "boe:BOE-A-2026-1", "watchlist": "inventada"},
    )
    assert datos["explicado"] is False
    assert "becas-informatica" in datos["error"]


async def test_explain_match_sin_watchlists_dice_como_crearlas(con_datos):
    datos = await llamar(construir_servidor(con_datos), "explain_match", {"id": "boe:BOE-A-2026-1"})
    assert datos["explicado"] is False
    assert "watchlists.example.yaml" in datos["error"]


async def test_explain_match_con_un_id_inexistente(con_datos, con_watchlists):
    datos = await llamar(construir_servidor(con_datos), "explain_match", {"id": "boe:BOE-A-1999-1"})
    assert datos["explicado"] is False


# --------------------------------------------------------------------------- #
# deadlines_soon
# --------------------------------------------------------------------------- #


async def test_deadlines_soon_devuelve_lo_que_vence_pronto(con_datos):
    datos = await llamar(construir_servidor(con_datos), "deadlines_soon", {"dias": 7})

    assert datos["total"] == 1
    resultado = datos["resultados"][0]
    assert resultado["identificador_oficial"] == "BOE-A-2026-2"
    assert resultado["dias_restantes"] == 3


async def test_deadlines_soon_respeta_la_ventana(con_datos):
    servidor = construir_servidor(con_datos)

    assert (await llamar(servidor, "deadlines_soon", {"dias": 1}))["total"] == 0
    assert (await llamar(servidor, "deadlines_soon", {"dias": 60}))["total"] == 2


async def test_deadlines_soon_avisa_de_que_no_es_exhaustivo(con_datos):
    # Las convocatorias de plazo relativo no salen, y el modelo tiene que
    # saberlo para no decir que no vence nada mas.
    datos = await llamar(construir_servidor(con_datos), "deadlines_soon")

    assert "2 de las 3 convocatorias" in datos["cobertura_de_plazos"]
    assert "días hábiles" in datos["cobertura_de_plazos"]
    assert "NO es exhaustiva" in datos["cobertura_de_plazos"]


async def test_deadlines_soon_omite_las_descartadas(con_datos):
    servidor = construir_servidor(con_datos)
    await llamar(servidor, "discard", {"id": "boe:BOE-A-2026-2", "motivo": "no me aplica"})

    datos = await llamar(servidor, "deadlines_soon", {"dias": 7})

    assert datos["total"] == 0


async def test_deadlines_soon_no_incluye_plazos_desconocidos(con_datos):
    # La de informatica no tiene plazo determinado: no puede aparecer como que
    # vence, ni como que no.
    datos = await llamar(construir_servidor(con_datos), "deadlines_soon", {"dias": 365})
    identificadores = {r["identificador_oficial"] for r in datos["resultados"]}
    assert "BOE-A-2026-1" not in identificadores


# --------------------------------------------------------------------------- #
# Seguimiento
# --------------------------------------------------------------------------- #


async def test_mark_solicitada_guarda_notas_y_fecha(con_datos):
    servidor = construir_servidor(con_datos)

    datos = await llamar(
        servidor,
        "mark_solicitada",
        {"id": "boe:BOE-A-2026-1", "notas": "Enviado por sede electrónica"},
    )

    assert datos["guardado"] is True
    assert datos["estado"] == "solicitada"

    listado = await llamar(servidor, "list_solicitudes")
    assert listado["total"] == 1
    assert listado["solicitadas"][0]["notas"] == "Enviado por sede electrónica"
    assert listado["solicitadas"][0]["fecha_solicitud"] == HOY.isoformat()


async def test_discard_guarda_el_motivo(con_datos):
    servidor = construir_servidor(con_datos)

    datos = await llamar(
        servidor, "discard", {"id": "boe:BOE-A-2026-1", "motivo": "pide experiencia"}
    )

    assert datos["guardado"] is True
    assert datos["estado"] == "descartada"

    listado = await llamar(servidor, "list_solicitudes", {"incluir_descartadas": True})
    assert listado["descartadas"][0]["motivo"] == "pide experiencia"


async def test_una_descartada_desaparece_de_las_busquedas(con_datos):
    servidor = construir_servidor(con_datos)
    antes = await llamar(servidor, "search_convocatorias")

    await llamar(servidor, "discard", {"id": "boe:BOE-A-2026-1"})
    despues = await llamar(servidor, "search_convocatorias")

    assert despues["total"] == antes["total"] - 1


async def test_marcar_un_id_que_no_existe_no_crea_nada(con_datos):
    servidor = construir_servidor(con_datos)

    datos = await llamar(servidor, "mark_solicitada", {"id": "boe:BOE-A-1999-1"})

    assert datos["guardado"] is False
    assert "search_convocatorias" in datos["error"]
    assert (await llamar(servidor, "list_solicitudes"))["total"] == 0


async def test_cambiar_de_estado_sustituye_el_anterior(con_datos):
    servidor = construir_servidor(con_datos)

    await llamar(servidor, "discard", {"id": "boe:BOE-A-2026-1", "motivo": "no me interesa"})
    await llamar(servidor, "mark_solicitada", {"id": "boe:BOE-A-2026-1"})

    listado = await llamar(servidor, "list_solicitudes", {"incluir_descartadas": True})
    assert listado["total"] == 1
    assert listado["descartadas"] == []


async def test_list_solicitudes_vacia_no_rompe(con_datos):
    datos = await llamar(construir_servidor(con_datos), "list_solicitudes")
    assert datos["total"] == 0
    assert datos["solicitadas"] == []


# --------------------------------------------------------------------------- #
# El ranking en la busqueda
# --------------------------------------------------------------------------- #


async def test_la_busqueda_ordena_por_score_cuando_hay_watchlists(con_datos, con_watchlists):
    datos = await llamar(construir_servidor(con_datos), "search_convocatorias", {"limite": 10})

    orden = [r["identificador_oficial"] for r in datos["resultados"]]
    # Las dos que coinciden con una watchlist van delante; la de ganaderia, al
    # final. Entre las dos empatan a 1.0, asi que desempata el orden canonico
    # por seccion (II.B antes de III), que es determinista.
    assert orden == ["BOE-A-2026-2", "BOE-A-2026-1", "BOE-A-2026-3"]
    assert datos["resultados"][0]["score"] == 1.0
    assert datos["resultados"][0]["watchlists"] == ["empleo-tic"]
    assert datos["resultados"][1]["watchlists"] == ["becas-informatica"]
    # La de ganadería está en una sección vigilada pero no casa ninguna
    # keyword, así que no coincide con ninguna watchlist y se va al final.
    assert datos["resultados"][2]["watchlists"] == []
    assert datos["resultados"][2]["score"] is None


async def test_el_orden_con_watchlists_es_estable(con_datos, con_watchlists):
    servidor = construir_servidor(con_datos)
    primera = await llamar(servidor, "search_convocatorias", {"limite": 10})
    segunda = await llamar(servidor, "search_convocatorias", {"limite": 10})
    assert [r["id"] for r in primera["resultados"]] == [r["id"] for r in segunda["resultados"]]


async def test_filtrar_por_watchlist_deja_solo_lo_que_encaja(con_datos, con_watchlists):
    datos = await llamar(
        construir_servidor(con_datos),
        "search_convocatorias",
        {"watchlist": "becas-informatica"},
    )

    assert datos["total"] == 1
    assert datos["resultados"][0]["identificador_oficial"] == "BOE-A-2026-1"


async def test_una_watchlist_inexistente_lo_dice(con_datos, con_watchlists):
    datos = await llamar(
        construir_servidor(con_datos), "search_convocatorias", {"watchlist": "inventada"}
    )

    assert datos["total"] == 0
    assert "becas-informatica" in datos["error"]


async def test_sin_watchlists_la_busqueda_sigue_funcionando(con_datos):
    datos = await llamar(construir_servidor(con_datos), "search_convocatorias")

    assert datos["total"] == 3
    assert datos["resultados"][0]["score"] is None
