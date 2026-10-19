"""Tests del índice SQLite.

El esquema es la base de dos propiedades que el proyecto necesita: que la
ingesta sea idempotente (la clave es el identificador oficial) y que sepa qué
fechas le faltan (``source_health``). Aquí se verifican ambas, además de que
los ``ON DELETE CASCADE`` funcionan de verdad, que en SQLite no es gratis.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from convocatorias_mcp.db import (
    SCHEMA_VERSION,
    IndexSchemaError,
    estado_del_indice,
    initialize,
    open_index,
    schema_version,
)

AHORA = datetime(2026, 10, 19, 10, 0, tzinfo=UTC).isoformat()


@pytest.fixture
def conn(tmp_path: Path):
    conexion = open_index(tmp_path / "indice.sqlite3")
    yield conexion
    conexion.close()


def insertar_convocatoria(conn: sqlite3.Connection, identificador: str, fecha: str) -> str:
    convocatoria_id = f"boe:{identificador}"
    conn.execute(
        """
        INSERT INTO convocatorias (
            id, fuente, identificador, titulo, titulo_normalizado, seccion,
            fecha_publicacion, url_html, url_xml, ingerido_at, actualizado_at
        ) VALUES (?, 'boe', ?, ?, ?, 'II.B', ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            titulo = excluded.titulo,
            actualizado_at = excluded.actualizado_at
        """,
        (
            convocatoria_id,
            identificador,
            f"Resolución {identificador}",
            f"resolucion {identificador.lower()}",
            fecha,
            f"https://www.boe.es/diario_boe/txt.php?id={identificador}",
            f"https://www.boe.es/diario_boe/xml.php?id={identificador}",
            AHORA,
            AHORA,
        ),
    )
    return convocatoria_id


def registrar_dia(conn: sqlite3.Connection, fecha: str, estado: str = "ok", items: int = 1) -> None:
    conn.execute(
        """
        INSERT INTO source_health (fuente, fecha, estado, items, ingerido_at)
        VALUES ('boe', ?, ?, ?, ?)
        ON CONFLICT (fuente, fecha) DO UPDATE SET
            estado = excluded.estado, items = excluded.items
        """,
        (fecha, estado, items, AHORA),
    )


# --------------------------------------------------------------------------- #
# Esquema
# --------------------------------------------------------------------------- #


def test_open_index_crea_el_esquema(conn: sqlite3.Connection):
    tablas = {
        fila["name"] for fila in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "meta",
        "convocatorias",
        "convocatoria_watchlists",
        "seguimiento",
        "source_health",
        "digest_envios",
    } <= tablas
    assert schema_version(conn) == SCHEMA_VERSION


def test_initialize_es_idempotente(conn: sqlite3.Connection):
    initialize(conn)
    initialize(conn)
    assert schema_version(conn) == SCHEMA_VERSION


def test_un_esquema_de_otra_version_avisa_en_vez_de_corromperse(conn: sqlite3.Connection):
    conn.execute("UPDATE meta SET valor = '99' WHERE clave = 'schema_version'")
    with pytest.raises(IndexSchemaError, match="v99"):
        initialize(conn)


def test_las_claves_ajenas_estan_activadas(conn: sqlite3.Connection):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_el_journal_es_wal(conn: sqlite3.Connection):
    # Permite que el export del dashboard lea mientras la ingesta escribe.
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_open_index_crea_las_carpetas_que_falten(tmp_path: Path):
    destino = tmp_path / "sin" / "crear" / "indice.sqlite3"
    conexion = open_index(destino)
    conexion.close()
    assert destino.is_file()


# --------------------------------------------------------------------------- #
# Idempotencia
# --------------------------------------------------------------------------- #


def test_reingerir_la_misma_convocatoria_no_duplica(conn: sqlite3.Connection):
    insertar_convocatoria(conn, "BOE-A-2026-21635", "2026-10-17")
    insertar_convocatoria(conn, "BOE-A-2026-21635", "2026-10-17")
    assert conn.execute("SELECT COUNT(*) FROM convocatorias").fetchone()[0] == 1


def test_el_identificador_es_unico(conn: sqlite3.Connection):
    insertar_convocatoria(conn, "BOE-A-2026-21635", "2026-10-17")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO convocatorias (
                id, fuente, identificador, titulo, titulo_normalizado, seccion,
                fecha_publicacion, url_html, url_xml, ingerido_at, actualizado_at
            ) VALUES ('otro-id', 'boe', 'BOE-A-2026-21635', 't', 't', 'II.B',
                      '2026-10-17', 'https://x', 'https://x', ?, ?)
            """,
            (AHORA, AHORA),
        )


def test_reingerir_un_dia_no_duplica_su_salud(conn: sqlite3.Connection):
    registrar_dia(conn, "2026-10-17")
    registrar_dia(conn, "2026-10-17", items=270)
    filas = conn.execute("SELECT fecha, items FROM source_health").fetchall()
    assert len(filas) == 1
    assert filas[0]["items"] == 270


def test_borrar_una_convocatoria_arrastra_sus_dependientes(conn: sqlite3.Connection):
    convocatoria_id = insertar_convocatoria(conn, "BOE-A-2026-21635", "2026-10-17")
    conn.execute(
        "INSERT INTO convocatoria_watchlists (convocatoria_id, watchlist, score) VALUES (?, ?, ?)",
        (convocatoria_id, "becas", 0.8),
    )
    conn.execute(
        "INSERT INTO seguimiento (convocatoria_id, estado, actualizado_at) VALUES (?, ?, ?)",
        (convocatoria_id, "vista", AHORA),
    )

    conn.execute("DELETE FROM convocatorias WHERE id = ?", (convocatoria_id,))

    assert conn.execute("SELECT COUNT(*) FROM convocatoria_watchlists").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM seguimiento").fetchone()[0] == 0


def test_una_convocatoria_no_puede_coincidir_dos_veces_con_la_misma_watchlist(
    conn: sqlite3.Connection,
):
    convocatoria_id = insertar_convocatoria(conn, "BOE-A-2026-21635", "2026-10-17")
    conn.execute(
        "INSERT INTO convocatoria_watchlists (convocatoria_id, watchlist, score) VALUES (?, ?, ?)",
        (convocatoria_id, "becas", 0.8),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO convocatoria_watchlists (convocatoria_id, watchlist, score) "
            "VALUES (?, ?, ?)",
            (convocatoria_id, "becas", 0.9),
        )


# --------------------------------------------------------------------------- #
# Estado del indice
# --------------------------------------------------------------------------- #


def test_un_indice_vacio_lo_dice_claramente(conn: sqlite3.Connection):
    estado = estado_del_indice(conn)
    assert estado.esta_vacio
    assert estado.dias_registrados == 0
    assert estado.ultima_fecha is None
    assert "ingest" in estado.resumen()


def test_cuenta_dias_por_estado(conn: sqlite3.Connection):
    registrar_dia(conn, "2026-10-15", "ok", 100)
    registrar_dia(conn, "2026-10-16", "ok", 120)
    registrar_dia(conn, "2026-10-17", "sin_publicacion", 0)
    registrar_dia(conn, "2026-10-18", "error", 0)
    insertar_convocatoria(conn, "BOE-A-2026-21635", "2026-10-16")

    estado = estado_del_indice(conn)

    assert estado.dias_ok == 2
    assert estado.dias_sin_publicacion == 1
    assert estado.dias_con_error == 1
    assert estado.dias_registrados == 4
    assert estado.primera_fecha == date(2026, 10, 15)
    assert estado.ultima_fecha == date(2026, 10, 18)
    assert estado.convocatorias == 1
    assert not estado.esta_vacio


def test_detecta_los_huecos_del_rango(conn: sqlite3.Connection):
    registrar_dia(conn, "2026-10-15")
    registrar_dia(conn, "2026-10-18")

    estado = estado_del_indice(conn)

    assert estado.huecos == [date(2026, 10, 16), date(2026, 10, 17)]
    assert "2 huecos" in estado.resumen()


def test_un_rango_completo_no_tiene_huecos(conn: sqlite3.Connection):
    # Incluye un domingo registrado como sin publicacion: ingerido no es hueco.
    registrar_dia(conn, "2026-10-17", "ok", 270)
    registrar_dia(conn, "2026-10-18", "sin_publicacion", 0)
    registrar_dia(conn, "2026-10-19", "ok", 180)

    estado = estado_del_indice(conn)

    assert estado.huecos == []
    assert "sin publicación" in estado.resumen()


def test_el_estado_se_puede_pedir_por_fuente(conn: sqlite3.Connection):
    registrar_dia(conn, "2026-10-17")
    conn.execute(
        "INSERT INTO source_health (fuente, fecha, estado, items, ingerido_at) "
        "VALUES ('bocm', '2026-10-17', 'ok', 5, ?)",
        (AHORA,),
    )

    assert estado_del_indice(conn).dias_registrados == 2
    assert estado_del_indice(conn, fuente="boe").dias_registrados == 1
    assert estado_del_indice(conn, fuente="bocm").dias_registrados == 1
