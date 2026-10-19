"""Tests de la ingesta incremental.

Las tres propiedades que pide el proyecto: incremental (no repite fechas ya
resueltas), idempotente (reingerir no duplica) y **un día sin publicación no
cuenta como error**, que es lo que evita que los domingos ensucien la salud de
la fuente.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from convocatorias_mcp.db import estado_del_indice, open_index
from convocatorias_mcp.ingest import ingerir, rango_de_fechas
from convocatorias_mcp.models import Convocatoria
from convocatorias_mcp.sources.base import (
    BoletinSource,
    Consentimiento,
    SinPublicacion,
    SourceError,
)
from convocatorias_mcp.sources.fixture import FixtureSource

FECHA_CON_SUMARIO = date(2026, 10, 17)


@pytest.fixture
def conn(tmp_path: Path):
    conexion = open_index(tmp_path / "indice.sqlite3")
    yield conexion
    conexion.close()


class FuenteDeMentira(BoletinSource):
    """Fuente controlada: se le dice qué devolver para cada fecha."""

    name = "mentira"
    display_name = "Fuente de mentira"
    base_url = "https://ejemplo.test"
    method = "api"
    consentimiento = Consentimiento.DATOS_ABIERTOS

    def __init__(self, respuestas: dict[date, object]) -> None:
        self.respuestas = respuestas
        self.pedidas: list[date] = []

    async def fetch_dia(self, fecha: date) -> list[Convocatoria]:
        self.pedidas.append(fecha)
        respuesta = self.respuestas.get(fecha)
        if respuesta is None:
            raise SinPublicacion(self.name, fecha)
        if isinstance(respuesta, Exception):
            raise respuesta
        return list(respuesta)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Rango de fechas
# --------------------------------------------------------------------------- #


def test_el_rango_va_de_lo_mas_nuevo_a_lo_mas_viejo():
    # Una ingesta interrumpida debe dejar lo mas util: lo recien publicado.
    fechas = rango_de_fechas(date(2026, 10, 15), date(2026, 10, 17))
    assert fechas == [date(2026, 10, 17), date(2026, 10, 16), date(2026, 10, 15)]


def test_un_rango_de_un_solo_dia():
    assert rango_de_fechas(FECHA_CON_SUMARIO, FECHA_CON_SUMARIO) == [FECHA_CON_SUMARIO]


def test_un_rango_al_reves_esta_vacio():
    assert rango_de_fechas(date(2026, 10, 17), date(2026, 10, 15)) == []


# --------------------------------------------------------------------------- #
# Con la fuente de fixtures
# --------------------------------------------------------------------------- #


async def test_ingiere_el_sumario_real_y_registra_el_dia(conn):
    resumen = await ingerir(conn, FixtureSource(), desde=FECHA_CON_SUMARIO, hasta=FECHA_CON_SUMARIO)

    assert resumen.convocatorias == 271
    assert resumen.dias_pedidos == 1
    assert not resumen.errores

    estado = estado_del_indice(conn)
    assert estado.convocatorias == 271
    assert estado.dias_ok == 1
    assert estado.ultima_fecha == FECHA_CON_SUMARIO


async def test_los_dias_sin_sumario_se_registran_como_sin_publicacion(conn):
    resumen = await ingerir(
        conn, FixtureSource(), desde=date(2026, 10, 16), hasta=date(2026, 10, 18)
    )

    assert resumen.dias_sin_publicacion == 2
    assert resumen.convocatorias == 271
    # Y no cuentan como error: la fuente esta sana.
    assert not resumen.errores
    assert estado_del_indice(conn).dias_con_error == 0


async def test_reingerir_no_duplica_ni_vuelve_a_pedir(conn):
    fuente = FixtureSource()
    await ingerir(conn, fuente, desde=FECHA_CON_SUMARIO, hasta=FECHA_CON_SUMARIO)

    segunda = await ingerir(conn, fuente, desde=FECHA_CON_SUMARIO, hasta=FECHA_CON_SUMARIO)

    assert segunda.dias_pedidos == 0
    assert segunda.dias_omitidos == 1
    assert segunda.convocatorias == 0
    assert estado_del_indice(conn).convocatorias == 271


async def test_forzar_vuelve_a_pedir_las_fechas_resueltas(conn):
    fuente = FixtureSource()
    await ingerir(conn, fuente, desde=FECHA_CON_SUMARIO, hasta=FECHA_CON_SUMARIO)

    segunda = await ingerir(
        conn, fuente, desde=FECHA_CON_SUMARIO, hasta=FECHA_CON_SUMARIO, forzar=True
    )

    assert segunda.dias_pedidos == 1
    assert segunda.convocatorias == 271
    # Sigue sin duplicar: la clave es el identificador oficial.
    assert estado_del_indice(conn).convocatorias == 271


# --------------------------------------------------------------------------- #
# Resiliencia
# --------------------------------------------------------------------------- #


async def test_un_dia_que_falla_no_rompe_los_demas(conn, convocatoria_factory):
    fuente = FuenteDeMentira(
        {
            date(2026, 10, 17): [convocatoria_factory(fuente="mentira")],
            date(2026, 10, 16): SourceError("mentira", "HTTP 503", status_code=503),
        }
    )

    resumen = await ingerir(conn, fuente, desde=date(2026, 10, 15), hasta=date(2026, 10, 17))

    assert resumen.convocatorias == 1
    assert resumen.dias_sin_publicacion == 1
    assert [fecha for fecha, _ in resumen.errores] == [date(2026, 10, 16)]
    assert "503" in resumen.errores[0][1]

    estado = estado_del_indice(conn)
    assert estado.dias_ok == 1
    assert estado.dias_con_error == 1
    assert estado.dias_sin_publicacion == 1


async def test_un_dia_con_error_se_reintenta_en_la_siguiente_ingesta(conn, convocatoria_factory):
    fuente = FuenteDeMentira({FECHA_CON_SUMARIO: SourceError("mentira", "timeout")})
    await ingerir(conn, fuente, desde=FECHA_CON_SUMARIO, hasta=FECHA_CON_SUMARIO)

    fuente.respuestas[FECHA_CON_SUMARIO] = [convocatoria_factory(fuente="mentira")]
    fuente.pedidas.clear()
    segunda = await ingerir(conn, fuente, desde=FECHA_CON_SUMARIO, hasta=FECHA_CON_SUMARIO)

    assert fuente.pedidas == [FECHA_CON_SUMARIO]
    assert segunda.convocatorias == 1
    assert estado_del_indice(conn).dias_con_error == 0


async def test_el_resumen_se_lee_en_una_linea(conn):
    resumen = await ingerir(
        conn, FixtureSource(), desde=date(2026, 10, 16), hasta=date(2026, 10, 18)
    )
    texto = resumen.resumen()
    assert "271 convocatorias" in texto
    assert "sin publicación" in texto
