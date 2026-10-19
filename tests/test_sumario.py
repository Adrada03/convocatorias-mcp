"""Tests del parseo del sumario y de la fuente de fixtures.

El parser se prueba contra el sumario **real** del BOE guardado en
``tests/fixtures/``, no contra un JSON de juguete: los tres detalles que la
documentación no deja claros (niveles que llegan como objeto en vez de lista,
``epigrafe`` ausente y ``url_pdf`` como objeto) solo aparecen en los datos de
verdad.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from convocatorias_mcp.models import Seccion
from convocatorias_mcp.sources.base import SinPublicacion, SourceError
from convocatorias_mcp.sources.fixture import FixtureSource
from convocatorias_mcp.sources.sumario import (
    ParseoSumarioError,
    fecha_del_sumario,
    parsear_sumario,
)

FECHA_FIXTURE = date(2026, 10, 17)


# --------------------------------------------------------------------------- #
# El sumario real
# --------------------------------------------------------------------------- #


def test_parsea_el_sumario_real_completo(sumario_boe):
    convocatorias = parsear_sumario(sumario_boe, fuente="boe")

    # El dia 17/10/2026 publico 270 items; si cambia el parser, cambia esto.
    assert len(convocatorias) == 270
    assert all(convocatoria.fuente == "boe" for convocatoria in convocatorias)
    assert all(convocatoria.fecha_publicacion == FECHA_FIXTURE for convocatoria in convocatorias)


def test_la_fecha_sale_de_los_metadatos_del_sumario(sumario_boe):
    assert fecha_del_sumario(sumario_boe) == FECHA_FIXTURE


def test_mapea_las_secciones_del_sumario_real(sumario_boe):
    convocatorias = parsear_sumario(sumario_boe, fuente="boe")
    secciones = {convocatoria.seccion for convocatoria in convocatorias}

    # Ninguna debe quedar sin reconocer: seria el fallo silencioso que haria
    # que las watchlists no coincidieran nunca con nada.
    assert Seccion.DESCONOCIDA not in secciones
    assert Seccion.II_B in secciones
    assert Seccion.III in secciones


def test_recupera_los_campos_de_una_convocatoria_concreta(sumario_boe):
    convocatorias = parsear_sumario(sumario_boe, fuente="boe")
    por_id = {convocatoria.identificador: convocatoria for convocatoria in convocatorias}

    convocatoria = por_id["BOE-A-2026-21635"]
    assert convocatoria.seccion is Seccion.II_B
    assert convocatoria.departamento == "MINISTERIO DE DEFENSA"
    assert convocatoria.epigrafe == "Procedimientos de libre designación"
    assert convocatoria.control == "2026/15820"
    assert convocatoria.url_html.endswith("id=BOE-A-2026-21635")
    # url_pdf viene dentro de un objeto, en su clave "texto".
    assert convocatoria.url_pdf is not None
    assert convocatoria.url_pdf.endswith(".pdf")
    # El sumario no trae ni texto ni plazo: no se inventan.
    assert convocatoria.texto_completo is None
    assert convocatoria.plazo_fin is None
    assert convocatoria.plazo_desconocido


def test_hay_items_sin_epigrafe_en_el_sumario_real(sumario_boe):
    # La seccion V.B tiene departamentos con item directamente debajo.
    convocatorias = parsear_sumario(sumario_boe, fuente="boe")
    assert any(convocatoria.epigrafe is None for convocatoria in convocatorias)
    assert any(convocatoria.epigrafe is not None for convocatoria in convocatorias)


def test_el_parseo_es_estable(sumario_boe):
    primero = parsear_sumario(sumario_boe, fuente="boe")
    segundo = parsear_sumario(sumario_boe, fuente="boe")
    assert [c.id for c in primero] == [c.id for c in segundo]


# --------------------------------------------------------------------------- #
# Formas raras del JSON
# --------------------------------------------------------------------------- #


def _sumario(diario: object, fecha: str = "20261017") -> dict:
    return {
        "status": {"code": 200},
        "data": {"sumario": {"metadatos": {"fecha_publicacion": fecha}, "diario": diario}},
    }


ITEM = {
    "identificador": "BOE-A-2026-1",
    "titulo": "Convocatoria de prueba",
    "url_html": "https://www.boe.es/txt/1",
    "url_xml": "https://www.boe.es/xml/1",
}


def test_acepta_un_nivel_como_objeto_en_vez_de_lista():
    # La API devuelve objeto suelto cuando hay un solo elemento.
    payload = _sumario(
        {
            "numero": "250",
            "seccion": {
                "codigo": "3",
                "nombre": "III. Otras disposiciones",
                "departamento": {
                    "nombre": "MINISTERIO",
                    "epigrafe": {"nombre": "Becas", "item": ITEM},
                },
            },
        }
    )
    convocatorias = parsear_sumario(payload, fuente="boe")
    assert len(convocatorias) == 1
    assert convocatorias[0].seccion is Seccion.III
    assert convocatorias[0].epigrafe == "Becas"


def test_acepta_items_directamente_bajo_el_departamento():
    payload = _sumario(
        [{"seccion": [{"codigo": "5B", "departamento": [{"nombre": "HACIENDA", "item": [ITEM]}]}]}]
    )
    convocatorias = parsear_sumario(payload, fuente="boe")
    assert len(convocatorias) == 1
    assert convocatorias[0].epigrafe is None


def test_descarta_items_sin_lo_imprescindible_sin_romper_el_dia():
    # Perder una convocatoria es mejor que perder las 270 del dia.
    payload = _sumario(
        [
            {
                "seccion": [
                    {
                        "codigo": "3",
                        "departamento": [
                            {
                                "nombre": "MINISTERIO",
                                "item": [
                                    ITEM,
                                    {"titulo": "sin identificador"},
                                    {"identificador": "BOE-A-2026-2", "titulo": "sin urls"},
                                    "una cadena en vez de un objeto",
                                ],
                            }
                        ],
                    }
                ]
            }
        ]
    )
    convocatorias = parsear_sumario(payload, fuente="boe")
    assert [c.identificador for c in convocatorias] == ["BOE-A-2026-1"]


def test_un_item_sin_titulo_usa_su_identificador():
    item = {k: v for k, v in ITEM.items() if k != "titulo"}
    payload = _sumario([{"seccion": [{"codigo": "3", "departamento": [{"item": [item]}]}]}])
    assert parsear_sumario(payload, fuente="boe")[0].titulo == "BOE-A-2026-1"


def test_un_sumario_vacio_devuelve_lista_vacia():
    assert parsear_sumario(_sumario([]), fuente="boe") == []


def test_un_json_que_no_es_un_sumario_avisa():
    with pytest.raises(ParseoSumarioError, match=r"data\.sumario"):
        parsear_sumario({"otra": "cosa"}, fuente="boe")

    with pytest.raises(ParseoSumarioError, match="objeto JSON"):
        parsear_sumario([1, 2, 3], fuente="boe")


def test_sin_fecha_en_los_metadatos_se_usa_la_pedida():
    payload = _sumario([], fecha="")
    assert parsear_sumario(payload, fuente="boe", fecha=FECHA_FIXTURE) == []
    with pytest.raises(ParseoSumarioError, match="no declara fecha"):
        parsear_sumario(payload, fuente="boe")


def test_una_fecha_mal_formada_en_los_metadatos_no_rompe():
    assert fecha_del_sumario(_sumario([], fecha="no-es-fecha")) is None


# --------------------------------------------------------------------------- #
# FixtureSource
# --------------------------------------------------------------------------- #


async def test_la_fuente_de_fixtures_lee_el_sumario_guardado():
    convocatorias = await FixtureSource().fetch_dia(FECHA_FIXTURE)
    assert len(convocatorias) == 270
    assert all(convocatoria.fuente == "fixture" for convocatoria in convocatorias)


async def test_una_fecha_sin_fichero_es_un_dia_sin_publicacion():
    # Se comporta como el BOE un domingo, para que la ingesta no note la
    # diferencia entre esta fuente y la real.
    with pytest.raises(SinPublicacion) as exc_info:
        await FixtureSource().fetch_dia(date(2026, 10, 18))
    assert exc_info.value.fecha == date(2026, 10, 18)


def test_la_fuente_declara_la_licencia_y_la_cita_del_boe():
    # La licencia del BOE obliga a citar la fuente: se declara aqui para que
    # ninguna salida pueda olvidarla.
    assert FixtureSource.licencia_url is not None
    assert "boe.es" in FixtureSource.licencia_url
    assert FixtureSource.cita_requerida is not None
    assert "Boletín Oficial del Estado" in FixtureSource.cita_requerida


def test_las_fechas_disponibles_se_listan():
    assert FECHA_FIXTURE in FixtureSource().fechas_disponibles()


async def test_un_fichero_corrupto_da_source_error(tmp_path: Path):
    (tmp_path / "boe_sumario_20261017.json").write_text("{roto", encoding="utf-8")
    fuente = FixtureSource(directorio=tmp_path)
    with pytest.raises(SourceError, match="no se pudo leer"):
        await fuente.fetch_dia(FECHA_FIXTURE)


async def test_un_json_valido_que_no_es_sumario_da_source_error(tmp_path: Path):
    (tmp_path / "boe_sumario_20261017.json").write_text('{"otra": "cosa"}', encoding="utf-8")
    fuente = FixtureSource(directorio=tmp_path)
    with pytest.raises(SourceError, match="forma de sumario"):
        await fuente.fetch_dia(FECHA_FIXTURE)


def test_un_directorio_que_no_existe_hace_la_fuente_no_disponible(tmp_path: Path):
    assert not FixtureSource(directorio=tmp_path / "no-existe").is_available()
    assert FixtureSource().is_available()
