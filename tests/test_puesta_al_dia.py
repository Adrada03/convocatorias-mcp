"""Tests de la puesta al día automática del servidor.

El reparto que se verifica: un atraso pequeño se ingiere antes de responder,
uno grande no bloquea la consulta pero avisa, y en los dos casos la respuesta
dice hasta qué fecha llegan los datos. Es lo que evita que el modelo afirme
"no hay convocatorias" cuando lo que pasa es que no ha mirado.

Se usa la fuente de desarrollo, que lee sumarios de disco, así que la ingesta
de prueba no toca la red.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from convocatorias_mcp.config import Settings
from convocatorias_mcp.db import open_index
from convocatorias_mcp.index import guardar_convocatorias
from convocatorias_mcp.server import construir_servidor

HOY = date(2026, 10, 19)
AHORA = datetime(2026, 10, 19, 10, 0, tzinfo=UTC).isoformat()
# El unico dia con sumario guardado dentro del rango de estos tests.
DIA_CON_FIXTURE = date(2026, 10, 17)


@pytest.fixture(autouse=True)
def _hoy_fijo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Congela "hoy" para que el atraso no dependa del día en que se ejecute."""
    monkeypatch.setattr("convocatorias_mcp.server.hoy_boletin", lambda: HOY)


def settings_para(tmp_path: Path, **overrides: object) -> Settings:
    data: dict[str, object] = {
        "watchlists_path": tmp_path / "watchlists.yaml",
        "index_path": tmp_path / "indice.sqlite3",
    }
    data.update(overrides)
    return Settings(**data)  # type: ignore[arg-type]


def poblar(settings: Settings, convocatoria_factory, *, hasta: date, dias: int = 3) -> None:
    """Deja el índice con datos y con registro de ingesta hasta ``hasta``."""
    conn = open_index(settings.index_path)
    try:
        guardar_convocatorias(
            conn,
            [
                convocatoria_factory(
                    fuente="fixture",
                    identificador=f"BOE-A-2026-{100 + indice}",
                    fecha_publicacion=hasta - timedelta(days=indice),
                )
                for indice in range(dias)
            ],
        )
        for indice in range(dias):
            conn.execute(
                "INSERT INTO source_health (fuente, fecha, estado, items, ingerido_at) "
                "VALUES ('fixture', ?, 'ok', 1, ?)",
                ((hasta - timedelta(days=indice)).isoformat(), AHORA),
            )
    finally:
        conn.close()


def dias_registrados(settings: Settings) -> list[str]:
    conn = open_index(settings.index_path)
    try:
        return [
            fila["fecha"]
            for fila in conn.execute("SELECT fecha FROM source_health ORDER BY fecha")
        ]
    finally:
        conn.close()


async def llamar(servidor, nombre: str, argumentos: dict | None = None) -> dict:
    resultado = await servidor.call_tool(nombre, argumentos or {})
    return resultado.structured_content or {}


# --------------------------------------------------------------------------- #
# Al dia
# --------------------------------------------------------------------------- #


async def test_un_indice_al_dia_no_ingiere_nada(tmp_path: Path, convocatoria_factory):
    settings = settings_para(tmp_path)
    poblar(settings, convocatoria_factory, hasta=HOY)
    antes = dias_registrados(settings)

    datos = await llamar(construir_servidor(settings), "search_convocatorias")

    assert datos["indice_desactualizado"] is False
    assert datos["dias_de_atraso"] == 0
    assert datos["indice_cubre_hasta"] == HOY.isoformat()
    assert "aviso_indice" not in datos
    assert "puesta_al_dia" not in datos
    assert dias_registrados(settings) == antes


# --------------------------------------------------------------------------- #
# Atraso pequeno: se pone al dia antes de responder
# --------------------------------------------------------------------------- #


async def test_un_atraso_pequeno_se_ingiere_antes_de_responder(
    tmp_path: Path, convocatoria_factory
):
    settings = settings_para(tmp_path, auto_ingest_dias=7)
    # Ultimo dia comprobado: 16/10. Faltan 17, 18 y 19.
    poblar(settings, convocatoria_factory, hasta=date(2026, 10, 16))

    datos = await llamar(construir_servidor(settings), "search_convocatorias")

    assert "puesta_al_dia" in datos
    assert datos["indice_desactualizado"] is False
    assert datos["dias_de_atraso"] == 0
    assert datos["indice_cubre_hasta"] == HOY.isoformat()
    # Los tres dias que faltaban quedan registrados.
    registrados = dias_registrados(settings)
    assert "2026-10-17" in registrados
    assert "2026-10-18" in registrados
    assert "2026-10-19" in registrados


async def test_la_puesta_al_dia_trae_convocatorias_de_verdad(
    tmp_path: Path, convocatoria_factory
):
    settings = settings_para(tmp_path, auto_ingest_dias=7)
    poblar(settings, convocatoria_factory, hasta=date(2026, 10, 16))

    datos = await llamar(construir_servidor(settings), "search_convocatorias")

    # El 17/10 tiene sumario guardado: 271 convocatorias reales del BOE.
    assert "271 convocatorias nuevas" in datos["puesta_al_dia"]


async def test_los_dias_sin_publicacion_de_la_puesta_al_dia_no_son_error(
    tmp_path: Path, convocatoria_factory
):
    settings = settings_para(tmp_path, auto_ingest_dias=7)
    poblar(settings, convocatoria_factory, hasta=date(2026, 10, 16))

    datos = await llamar(construir_servidor(settings), "search_convocatorias")

    # El 18 y el 19 no tienen sumario guardado y se registran sin publicacion,
    # asi que el indice queda al dia igualmente.
    assert datos["indice_desactualizado"] is False
    assert "aviso_indice" not in datos


async def test_la_puesta_al_dia_solo_se_intenta_una_vez_por_proceso(
    tmp_path: Path, convocatoria_factory
):
    settings = settings_para(tmp_path, auto_ingest_dias=7)
    poblar(settings, convocatoria_factory, hasta=date(2026, 10, 16))
    servidor = construir_servidor(settings)

    primera = await llamar(servidor, "search_convocatorias")
    segunda = await llamar(servidor, "search_convocatorias")

    assert "puesta_al_dia" in primera
    # La segunda ya no tiene nada que hacer.
    assert "puesta_al_dia" not in segunda
    assert segunda["dias_de_atraso"] == 0


# --------------------------------------------------------------------------- #
# Atraso grande: no bloquea, avisa
# --------------------------------------------------------------------------- #


async def test_un_atraso_grande_no_bloquea_pero_avisa(tmp_path: Path, convocatoria_factory):
    settings = settings_para(tmp_path, auto_ingest_dias=7)
    poblar(settings, convocatoria_factory, hasta=date(2026, 9, 1))
    antes = dias_registrados(settings)

    datos = await llamar(construir_servidor(settings), "search_convocatorias")

    assert datos["indice_desactualizado"] is True
    assert datos["dias_de_atraso"] == 48
    assert datos["indice_cubre_hasta"] == "2026-09-01"
    assert "convocatorias-mcp ingest" in datos["aviso_indice"]
    assert "no afirmes que no hay nada" in datos["aviso_indice"]
    # Y no se ha puesto a descargar 48 dias en medio de una consulta.
    assert dias_registrados(settings) == antes
    assert "puesta_al_dia" not in datos


async def test_el_atraso_grande_sigue_devolviendo_resultados(
    tmp_path: Path, convocatoria_factory
):
    settings = settings_para(tmp_path, auto_ingest_dias=7)
    poblar(settings, convocatoria_factory, hasta=date(2026, 9, 1))

    datos = await llamar(construir_servidor(settings), "search_convocatorias")

    # Lo que haya se devuelve igual: el aviso acompaña, no sustituye.
    assert datos["total"] == 3


async def test_con_el_umbral_a_cero_no_se_pone_al_dia(tmp_path: Path, convocatoria_factory):
    settings = settings_para(tmp_path, auto_ingest_dias=0)
    poblar(settings, convocatoria_factory, hasta=date(2026, 10, 16))
    antes = dias_registrados(settings)

    datos = await llamar(construir_servidor(settings), "search_convocatorias")

    assert datos["indice_desactualizado"] is True
    assert dias_registrados(settings) == antes


# --------------------------------------------------------------------------- #
# Indice vacio o sin registro de ingesta
# --------------------------------------------------------------------------- #


async def test_un_indice_vacio_avisa_y_no_hace_el_backfill_entero(tmp_path: Path):
    settings = settings_para(tmp_path, auto_ingest_dias=7)

    datos = await llamar(construir_servidor(settings), "search_convocatorias")

    assert datos["indice_desactualizado"] is True
    assert datos["indice_cubre_hasta"] is None
    assert "convocatorias-mcp ingest" in datos["aviso_indice"]
    # Un backfill de 90 dias no cabe en una respuesta.
    assert dias_registrados(settings) == []


async def test_datos_sin_registro_de_ingesta_dicen_hasta_donde_llegan(
    tmp_path: Path, convocatoria_factory
):
    settings = settings_para(tmp_path)
    conn = open_index(settings.index_path)
    try:
        guardar_convocatorias(
            conn, [convocatoria_factory(fecha_publicacion=DIA_CON_FIXTURE)]
        )
    finally:
        conn.close()

    datos = await llamar(construir_servidor(settings), "search_convocatorias")

    assert datos["indice_cubre_hasta"] == DIA_CON_FIXTURE.isoformat()
    assert datos["indice_desactualizado"] is True
    assert "ningún registro de ingesta" in datos["aviso_indice"]


# --------------------------------------------------------------------------- #
# source_health informa siempre
# --------------------------------------------------------------------------- #


async def test_source_health_informa_siempre_de_la_antiguedad(
    tmp_path: Path, convocatoria_factory
):
    settings = settings_para(tmp_path, auto_ingest_dias=0)
    poblar(settings, convocatoria_factory, hasta=date(2026, 10, 10))

    datos = await llamar(construir_servidor(settings), "source_health")

    assert datos["dias_de_atraso"] == 9
    assert datos["indice_desactualizado"] is True
    assert datos["indice_cubre_hasta"] == "2026-10-10"
    assert "aviso_indice" in datos


async def test_source_health_con_el_indice_al_dia_no_avisa(
    tmp_path: Path, convocatoria_factory
):
    settings = settings_para(tmp_path)
    poblar(settings, convocatoria_factory, hasta=HOY)

    datos = await llamar(construir_servidor(settings), "source_health")

    assert datos["dias_de_atraso"] == 0
    assert datos["indice_desactualizado"] is False
    assert "aviso_indice" not in datos
