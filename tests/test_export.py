"""Tests del export del dashboard.

Lo que importa aquí: que no se escape ni un texto completo, que el recorte no
pierda lo que coincide con una watchlist, y que la atribución que exige la
licencia del BOE salga de la fuente y no de una constante copiada.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from convocatorias_mcp.db import open_index
from convocatorias_mcp.export import (
    URL_BASE,
    VENTANA_POR_DEFECTO,
    atribucion,
    construir_datos,
    exportar,
)
from convocatorias_mcp.index import guardar_convocatorias
from convocatorias_mcp.models import Estado, PlazoOrigen, Seccion, Watchlist, WatchlistsConfig

HOY = date(2026, 10, 19)
RECIENTE = date(2026, 10, 15)
ANTIGUA = date(2026, 8, 1)

CONFIG = WatchlistsConfig(
    watchlists=[Watchlist(nombre="becas-informatica", keywords=["informatica"], secciones=["III"])]
)


@pytest.fixture
def conn(tmp_path: Path):
    conexion = open_index(tmp_path / "indice.sqlite3")
    yield conexion
    conexion.close()


@pytest.fixture
def poblado(conn, convocatoria_factory):
    guardar_convocatorias(
        conn,
        [
            convocatoria_factory(
                identificador="BOE-A-2026-1",
                titulo="Resolución de trámite reciente",
                fecha_publicacion=RECIENTE,
                seccion=Seccion.I,
            ),
            convocatoria_factory(
                identificador="BOE-A-2026-2",
                titulo="Becas de informática",
                fecha_publicacion=ANTIGUA,
                seccion=Seccion.III,
                departamento="MINISTERIO DE CIENCIA",
                plazo_fin=date(2026, 11, 5),
                plazo_origen=PlazoOrigen.TEXTO,
            ),
            convocatoria_factory(
                identificador="BOE-A-2026-3",
                titulo="Subvenciones a la ganadería",
                fecha_publicacion=ANTIGUA,
                seccion=Seccion.III,
            ),
        ],
    )
    return conn


# --------------------------------------------------------------------------- #
# El recorte
# --------------------------------------------------------------------------- #


def test_el_recorte_conserva_lo_reciente_y_lo_que_coincide(poblado):
    datos, resumen = construir_datos(poblado, config=CONFIG, dias=30, hoy=HOY)

    identificadores = {c["i"] for c in datos["convocatorias"]}
    # La reciente entra por fecha; la beca antigua, por coincidir con la
    # watchlist. La de ganadería ni una cosa ni la otra.
    assert identificadores == {"BOE-A-2026-1", "BOE-A-2026-2"}
    assert resumen.recortado is True
    assert resumen.exportadas == 2
    assert resumen.total_indice == 3
    assert resumen.coincidentes == 1


def test_sin_recorte_se_exporta_todo(poblado):
    datos, resumen = construir_datos(poblado, config=CONFIG, dias=None, hoy=HOY)

    assert len(datos["convocatorias"]) == 3
    assert resumen.recortado is False
    assert datos["recorte"]["aplicado"] is False


def test_sin_watchlists_el_recorte_es_solo_la_ventana(poblado):
    datos, resumen = construir_datos(poblado, config=None, dias=30, hoy=HOY)

    assert {c["i"] for c in datos["convocatorias"]} == {"BOE-A-2026-1"}
    assert resumen.coincidentes == 0


def test_la_ventana_por_defecto_es_de_un_mes():
    assert VENTANA_POR_DEFECTO == 30


# --------------------------------------------------------------------------- #
# La forma de cada registro
# --------------------------------------------------------------------------- #


def test_las_claves_son_cortas_y_sin_campos_vacios(poblado):
    datos, _ = construir_datos(poblado, config=CONFIG, dias=None, hoy=HOY)
    por_id = {c["i"]: c for c in datos["convocatorias"]}

    beca = por_id["BOE-A-2026-2"]
    assert beca["t"] == "Becas de informática"
    assert beca["f"] == ANTIGUA.isoformat()
    assert beca["s"] == "III"
    assert beca["d"] == "MINISTERIO DE CIENCIA"
    assert beca["p"] == "2026-11-05"
    assert beca["w"] == ["becas-informatica"]
    assert beca["sc"] > 0

    # "nueva" es el estado por defecto: no se repite en cada registro.
    assert "e" not in beca
    # Sin plazo ni watchlist, esas claves tampoco aparecen.
    ganaderia = por_id["BOE-A-2026-3"]
    assert "p" not in ganaderia
    assert "w" not in ganaderia


def test_el_estado_solo_aparece_cuando_no_es_nueva(poblado):
    poblado.execute(
        "INSERT INTO seguimiento (convocatoria_id, estado, actualizado_at) VALUES (?, ?, ?)",
        ("boe:BOE-A-2026-1", Estado.SOLICITADA.value, "2026-10-19T10:00:00+00:00"),
    )

    datos, _ = construir_datos(poblado, config=CONFIG, dias=None, hoy=HOY)
    por_id = {c["i"]: c for c in datos["convocatorias"]}

    assert por_id["BOE-A-2026-1"]["e"] == "solicitada"
    assert "e" not in por_id["BOE-A-2026-3"]


def test_no_se_exporta_la_url_porque_se_deriva(poblado):
    datos, _ = construir_datos(poblado, config=CONFIG, dias=None, hoy=HOY)

    assert datos["url_base"] == URL_BASE
    for registro in datos["convocatorias"]:
        assert "url" not in registro
        assert "u" not in registro
    # Y la página la reconstruye así.
    enlace = datos["url_base"] + datos["convocatorias"][0]["i"]
    assert enlace.startswith("https://www.boe.es/diario_boe/txt.php?id=BOE-")


def test_no_se_escapa_ningun_texto_completo(conn, convocatoria_factory):
    guardar_convocatorias(
        conn,
        [convocatoria_factory(texto_completo="De acuerdo con lo dispuesto en el artículo...")],
    )

    datos, _ = construir_datos(conn, dias=None, hoy=HOY)

    crudo = json.dumps(datos, ensure_ascii=False)
    assert "De acuerdo con lo dispuesto" not in crudo
    assert "texto" not in {clave for registro in datos["convocatorias"] for clave in registro}


# --------------------------------------------------------------------------- #
# Cabecera: cobertura y atribucion
# --------------------------------------------------------------------------- #


def test_la_cabecera_dice_hasta_donde_llegan_los_datos(poblado):
    datos, _ = construir_datos(poblado, config=CONFIG, dias=None, hoy=HOY)

    # Sin registro de ingesta se informa de la última publicación indexada.
    assert datos["cubre_hasta"] == RECIENTE.isoformat()


def test_la_atribucion_sale_de_la_fuente(poblado):
    datos, _ = construir_datos(poblado, config=CONFIG, dias=None, hoy=HOY)

    assert datos["fuentes"][0]["fuente"] == "boe"
    assert datos["fuentes"][0]["nombre"] == "Boletín Oficial del Estado"
    assert "Boletín Oficial del Estado" in datos["cita"]
    assert "aviso_legal" in datos["licencia"]


def test_la_atribucion_se_lee_del_registro_de_fuentes(poblado):
    from convocatorias_mcp.sources import BoeSource

    atribuciones = atribucion(poblado)

    # No es una constante copiada: es lo que declara la clase de la fuente.
    assert atribuciones[0]["cita"] == BoeSource.cita_requerida
    assert atribuciones[0]["licencia"] == BoeSource.licencia_url


def test_una_fuente_desconocida_no_deja_la_pagina_sin_cita(conn, convocatoria_factory):
    # Si el índice tiene datos de una fuente que ya no está registrada, la
    # licencia sigue obligando a citar.
    guardar_convocatorias(conn, [convocatoria_factory(fuente="bocm")])

    atribuciones = atribucion(conn)

    assert atribuciones[0]["fuente"] == "bocm"
    assert "Boletín Oficial del Estado" in atribuciones[0]["cita"]


def test_la_cabecera_trae_lo_que_la_pagina_necesita_para_sus_filtros(poblado):
    datos, _ = construir_datos(poblado, config=CONFIG, dias=None, hoy=HOY)

    assert datos["watchlists"] == ["becas-informatica"]
    assert datos["secciones"]["II.B"].startswith("II.B")
    assert "desconocida" not in datos["secciones"]
    assert datos["estados"] == ["nueva", "vista", "descartada", "solicitada"]


# --------------------------------------------------------------------------- #
# Escritura
# --------------------------------------------------------------------------- #


def test_exportar_escribe_un_json_valido(poblado, tmp_path: Path):
    destino = tmp_path / "salida" / "data.json"

    resumen = exportar(poblado, destino, config=CONFIG, dias=None, hoy=HOY)

    assert destino.is_file()
    datos = json.loads(destino.read_text(encoding="utf-8"))
    assert len(datos["convocatorias"]) == 3
    assert resumen.tamano_bytes == len(destino.read_bytes())
    assert resumen.ruta == destino


def test_exportar_crea_las_carpetas_que_falten(poblado, tmp_path: Path):
    destino = tmp_path / "sin" / "crear" / "data.json"
    exportar(poblado, destino, dias=None, hoy=HOY)
    assert destino.is_file()


def test_el_resumen_se_lee_en_una_linea(poblado, tmp_path: Path):
    resumen = exportar(poblado, tmp_path / "data.json", config=CONFIG, dias=30, hoy=HOY)

    texto = resumen.resumen()
    assert "2 de 3 convocatorias" in texto
    assert "últimos 30 días" in texto


def test_un_indice_vacio_exporta_una_lista_vacia(conn, tmp_path: Path):
    resumen = exportar(conn, tmp_path / "data.json", dias=None, hoy=HOY)

    assert resumen.exportadas == 0
    datos = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert datos["convocatorias"] == []
    assert datos["cubre_hasta"] is None


# --------------------------------------------------------------------------- #
# La nota de las watchlists
# --------------------------------------------------------------------------- #


def test_la_nota_de_las_watchlists_viaja_al_json(poblado):
    config = WatchlistsConfig(
        watchlists=[Watchlist(nombre="becas", keywords=["informatica"])],
        nota="Estas watchlists son de ejemplo.",
    )

    datos, _ = construir_datos(poblado, config=config, dias=None, hoy=HOY)

    assert datos["nota_watchlists"] == "Estas watchlists son de ejemplo."


def test_sin_nota_el_campo_queda_a_none(poblado):
    datos, _ = construir_datos(poblado, config=CONFIG, dias=None, hoy=HOY)
    assert datos["nota_watchlists"] is None


def test_sin_watchlists_tampoco_hay_nota(poblado):
    datos, _ = construir_datos(poblado, config=None, dias=None, hoy=HOY)
    assert datos["nota_watchlists"] is None
