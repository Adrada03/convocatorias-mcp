"""Tests del repositorio del índice.

Dos propiedades que el resto del proyecto da por supuestas:

* **Reingerir no destruye nada**, en particular un texto completo ya descargado.
* **El orden es determinista.** El BOE no publica hora, así que las 270
  convocatorias de un día empatan en fecha; sin desempate explícito dos
  consultas idénticas podrían devolver listas distintas.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from convocatorias_mcp.db import open_index
from convocatorias_mcp.index import (
    buscar,
    fechas_registradas,
    guardar_convocatorias,
    obtener_convocatoria,
    ordenar,
    registrar_ingesta,
)
from convocatorias_mcp.models import (
    Estado,
    EstadoIngesta,
    PlazoOrigen,
    SearchFilters,
    Seccion,
    SourceHealth,
)


@pytest.fixture
def conn(tmp_path: Path):
    conexion = open_index(tmp_path / "indice.sqlite3")
    yield conexion
    conexion.close()


# --------------------------------------------------------------------------- #
# Guardar y recuperar
# --------------------------------------------------------------------------- #


def test_guarda_y_recupera_una_convocatoria(conn, convocatoria_factory):
    original = convocatoria_factory()
    assert guardar_convocatorias(conn, [original]) == 1

    recuperada = obtener_convocatoria(conn, original.id)

    assert recuperada is not None
    assert recuperada.identificador == original.identificador
    assert recuperada.titulo == original.titulo
    assert recuperada.seccion is original.seccion
    assert recuperada.fecha_publicacion == original.fecha_publicacion
    assert recuperada.url_pdf == original.url_pdf
    assert recuperada.estado is Estado.NUEVA


def test_guardar_una_lista_vacia_no_hace_nada(conn):
    assert guardar_convocatorias(conn, []) == 0


def test_una_convocatoria_que_no_esta_devuelve_none(conn):
    assert obtener_convocatoria(conn, "boe:BOE-A-1999-1") is None


def test_reingerir_actualiza_en_vez_de_duplicar(conn, convocatoria_factory):
    guardar_convocatorias(conn, [convocatoria_factory(titulo="Título viejo")])
    guardar_convocatorias(conn, [convocatoria_factory(titulo="Título corregido")])

    assert conn.execute("SELECT COUNT(*) FROM convocatorias").fetchone()[0] == 1
    recuperada = obtener_convocatoria(conn, "boe:BOE-A-2026-21635")
    assert recuperada is not None
    assert recuperada.titulo == "Título corregido"


def test_reingerir_no_borra_un_texto_ya_descargado(conn, convocatoria_factory):
    # El sumario no trae textos: si el upsert los pisara con NULL, reingerir un
    # dia borraria todas las descargas.
    guardar_convocatorias(
        conn,
        [
            convocatoria_factory(
                texto_completo="De acuerdo con lo dispuesto...",
                texto_descargado_at=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
            )
        ],
    )

    guardar_convocatorias(conn, [convocatoria_factory(texto_completo=None)])

    recuperada = obtener_convocatoria(conn, "boe:BOE-A-2026-21635")
    assert recuperada is not None
    assert recuperada.texto_completo == "De acuerdo con lo dispuesto..."
    assert recuperada.texto_descargado_at is not None


def test_reingerir_no_borra_un_plazo_ya_extraido(conn, convocatoria_factory):
    guardar_convocatorias(
        conn,
        [convocatoria_factory(plazo_fin=date(2026, 11, 5), plazo_origen=PlazoOrigen.TEXTO)],
    )

    guardar_convocatorias(conn, [convocatoria_factory()])

    recuperada = obtener_convocatoria(conn, "boe:BOE-A-2026-21635")
    assert recuperada is not None
    assert recuperada.plazo_fin == date(2026, 11, 5)
    assert recuperada.plazo_origen is PlazoOrigen.TEXTO


def test_el_ambito_sobrevive_al_viaje_por_sqlite(conn, convocatoria_factory):
    guardar_convocatorias(conn, [convocatoria_factory(ambito=["Estatal", "Madrid"])])
    recuperada = obtener_convocatoria(conn, "boe:BOE-A-2026-21635")
    assert recuperada is not None
    assert recuperada.ambito == ["Estatal", "Madrid"]


# --------------------------------------------------------------------------- #
# Orden determinista
# --------------------------------------------------------------------------- #


def _lote(convocatoria_factory, cuantas: int = 12):
    """Convocatorias que empatan en fecha, como las de un día real."""
    secciones = [Seccion.V_B, Seccion.I, Seccion.III, Seccion.II_B]
    return [
        convocatoria_factory(
            identificador=f"BOE-A-2026-{21600 + indice}",
            seccion=secciones[indice % len(secciones)],
            fecha_publicacion=date(2026, 10, 17) if indice % 3 else date(2026, 10, 16),
        )
        for indice in range(cuantas)
    ]


def test_dos_consultas_identicas_devuelven_el_mismo_orden(conn, convocatoria_factory):
    guardar_convocatorias(conn, _lote(convocatoria_factory))
    filtros = SearchFilters(limite=50)

    primera = [convocatoria.id for convocatoria in buscar(conn, filtros)]
    segunda = [convocatoria.id for convocatoria in buscar(conn, filtros)]

    assert primera == segunda
    assert len(primera) == 12


def test_el_orden_sobrevive_a_reinsertar_en_otro_orden(conn, convocatoria_factory):
    # Si el orden dependiera del rowid, insertar al reves lo cambiaria.
    lote = _lote(convocatoria_factory)
    guardar_convocatorias(conn, lote)
    esperado = [convocatoria.id for convocatoria in buscar(conn, SearchFilters(limite=50))]

    conn.execute("DELETE FROM convocatorias")
    guardar_convocatorias(conn, list(reversed(lote)))

    assert [c.id for c in buscar(conn, SearchFilters(limite=50))] == esperado


def test_ordena_por_fecha_descendente_y_luego_seccion(conn, convocatoria_factory):
    guardar_convocatorias(conn, _lote(convocatoria_factory))

    resultados = buscar(conn, SearchFilters(limite=50))

    fechas = [convocatoria.fecha_publicacion for convocatoria in resultados]
    assert fechas == sorted(fechas, reverse=True)

    # Dentro de cada fecha, por orden de seccion del boletin.
    del_17 = [c for c in resultados if c.fecha_publicacion == date(2026, 10, 17)]
    ordenes = [convocatoria.seccion.orden for convocatoria in del_17]
    assert ordenes == sorted(ordenes)


def test_el_orden_en_memoria_coincide_con_el_de_sql(conn, convocatoria_factory):
    lote = _lote(convocatoria_factory)
    guardar_convocatorias(conn, lote)

    desde_sql = [convocatoria.id for convocatoria in buscar(conn, SearchFilters(limite=50))]
    en_memoria = [convocatoria.id for convocatoria in ordenar(lote)]

    assert desde_sql == en_memoria


def test_el_orden_alfabetico_de_las_secciones_es_el_del_boletin():
    # La consulta SQL ordena por el texto de la seccion. Esto lo verifica, para
    # que una seccion nueva no rompa el orden en silencio.
    por_texto = sorted(Seccion, key=lambda seccion: seccion.value)
    por_orden = sorted(Seccion, key=lambda seccion: seccion.orden)
    assert por_texto == por_orden


# --------------------------------------------------------------------------- #
# Filtros
# --------------------------------------------------------------------------- #


def test_filtra_por_palabras_clave_sin_acentos(conn, convocatoria_factory):
    guardar_convocatorias(
        conn,
        [
            convocatoria_factory(
                identificador="BOE-A-2026-1", titulo="Beca de Ingeniería Informática"
            ),
            convocatoria_factory(identificador="BOE-A-2026-2", titulo="Subvención a la ganadería"),
        ],
    )

    resultados = buscar(conn, SearchFilters(keywords="informatica"))

    assert [c.identificador for c in resultados] == ["BOE-A-2026-1"]


def test_las_palabras_clave_se_exigen_todas(conn, convocatoria_factory):
    guardar_convocatorias(
        conn,
        [
            convocatoria_factory(identificador="BOE-A-2026-1", titulo="Beca de informática"),
            convocatoria_factory(identificador="BOE-A-2026-2", titulo="Beca de historia"),
        ],
    )

    assert len(buscar(conn, SearchFilters(keywords="beca informatica"))) == 1
    assert len(buscar(conn, SearchFilters(keywords="beca"))) == 2


def test_filtra_por_seccion(conn, convocatoria_factory):
    guardar_convocatorias(
        conn,
        [
            convocatoria_factory(identificador="BOE-A-2026-1", seccion=Seccion.II_B),
            convocatoria_factory(identificador="BOE-A-2026-2", seccion=Seccion.III),
        ],
    )

    resultados = buscar(conn, SearchFilters(seccion=Seccion.II_B))

    assert [c.identificador for c in resultados] == ["BOE-A-2026-1"]


def test_filtra_por_rango_de_fechas(conn, convocatoria_factory):
    guardar_convocatorias(
        conn,
        [
            convocatoria_factory(
                identificador="BOE-A-2026-1", fecha_publicacion=date(2026, 10, 10)
            ),
            convocatoria_factory(
                identificador="BOE-A-2026-2", fecha_publicacion=date(2026, 10, 17)
            ),
        ],
    )

    assert len(buscar(conn, SearchFilters(desde=date(2026, 10, 15)))) == 1
    assert len(buscar(conn, SearchFilters(hasta=date(2026, 10, 15)))) == 1
    assert len(buscar(conn, SearchFilters())) == 2


def test_el_limite_se_respeta(conn, convocatoria_factory):
    guardar_convocatorias(conn, _lote(convocatoria_factory))
    assert len(buscar(conn, SearchFilters(limite=5))) == 5


def test_las_vistas_se_omiten_por_defecto(conn, convocatoria_factory):
    guardar_convocatorias(conn, [convocatoria_factory()])
    conn.execute(
        "INSERT INTO seguimiento (convocatoria_id, estado, actualizado_at) VALUES (?, ?, ?)",
        ("boe:BOE-A-2026-21635", Estado.VISTA.value, datetime.now(UTC).isoformat()),
    )

    assert buscar(conn, SearchFilters()) == []
    recuperadas = buscar(conn, SearchFilters(include_vistas=True))
    assert len(recuperadas) == 1
    assert recuperadas[0].estado is Estado.VISTA


def test_las_descartadas_se_omiten_por_defecto(conn, convocatoria_factory):
    guardar_convocatorias(conn, [convocatoria_factory()])
    conn.execute(
        "INSERT INTO seguimiento (convocatoria_id, estado, motivo, actualizado_at) "
        "VALUES (?, ?, ?, ?)",
        (
            "boe:BOE-A-2026-21635",
            Estado.DESCARTADA.value,
            "no me aplica",
            datetime.now(UTC).isoformat(),
        ),
    )

    assert buscar(conn, SearchFilters()) == []
    assert len(buscar(conn, SearchFilters(include_descartadas=True))) == 1


def test_filtrar_por_watchlist_sin_ranking_no_devuelve_nada(conn, convocatoria_factory):
    # Hasta que exista el ranking la tabla de coincidencias esta vacia.
    guardar_convocatorias(conn, [convocatoria_factory()])
    assert buscar(conn, SearchFilters(watchlist="becas")) == []


# --------------------------------------------------------------------------- #
# Salud de la ingesta
# --------------------------------------------------------------------------- #


def test_registra_y_lista_las_fechas_resueltas(conn):
    registrar_ingesta(
        conn,
        SourceHealth(fuente="boe", fecha=date(2026, 10, 17), estado=EstadoIngesta.OK, items=270),
    )
    registrar_ingesta(
        conn,
        SourceHealth(fuente="boe", fecha=date(2026, 10, 18), estado=EstadoIngesta.SIN_PUBLICACION),
    )

    fechas = fechas_registradas(conn, "boe")

    # Un dia sin publicacion tambien esta resuelto: no hay que volver a pedirlo.
    assert fechas == {date(2026, 10, 17), date(2026, 10, 18)}


def test_las_fechas_con_error_se_reintentan(conn):
    registrar_ingesta(
        conn,
        SourceHealth(
            fuente="boe",
            fecha=date(2026, 10, 17),
            estado=EstadoIngesta.ERROR,
            detalle="timeout",
        ),
    )
    assert fechas_registradas(conn, "boe") == set()


def test_registrar_el_mismo_dia_dos_veces_lo_actualiza(conn):
    for estado, items in ((EstadoIngesta.ERROR, 0), (EstadoIngesta.OK, 270)):
        registrar_ingesta(
            conn,
            SourceHealth(
                fuente="boe",
                fecha=date(2026, 10, 17),
                estado=estado,
                items=items,
                detalle="timeout" if estado is EstadoIngesta.ERROR else None,
            ),
        )

    filas = conn.execute("SELECT estado, items FROM source_health").fetchall()
    assert len(filas) == 1
    assert filas[0]["estado"] == "ok"
    assert filas[0]["items"] == 270


def test_las_fechas_son_por_fuente(conn):
    registrar_ingesta(
        conn, SourceHealth(fuente="boe", fecha=date(2026, 10, 17), estado=EstadoIngesta.OK)
    )
    assert fechas_registradas(conn, "bocm") == set()
