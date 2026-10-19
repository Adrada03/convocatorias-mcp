"""Tests de la extracción de plazos.

La regla que se verifica una y otra vez: **ante la duda, None**. Un plazo
equivocado es peor que no tener plazo, porque alguien se fiaría de él y
perdería la convocatoria.
"""

from __future__ import annotations

from datetime import date

import pytest

from convocatorias_mcp.models import PlazoOrigen
from convocatorias_mcp.plazos import extraer_plazo

PUBLICADO = date(2026, 10, 17)


def extraer(texto: str | None):
    return extraer_plazo(texto, publicado=PUBLICADO)


# --------------------------------------------------------------------------- #
# Lo que si se extrae
# --------------------------------------------------------------------------- #


def test_una_fecha_explicita_de_presentacion():
    fecha, origen = extraer(
        "El plazo de presentación de solicitudes finalizará el 15 de noviembre de 2026."
    )
    assert fecha == date(2026, 11, 15)
    assert origen is PlazoOrigen.TEXTO


@pytest.mark.parametrize(
    "texto",
    [
        "El plazo de presentación de instancias termina el 30 de noviembre de 2026.",
        "Las solicitudes se presentarán hasta el 30 de noviembre de 2026.",
        "El plazo de inscripción finaliza el 30/11/2026.",
        "Plazo de presentación de solicitudes: hasta el 30 de noviembre de 2026.",
    ],
)
def test_distintas_formas_de_decir_lo_mismo(texto: str):
    fecha, origen = extraer(texto)
    assert fecha == date(2026, 11, 30)
    assert origen is PlazoOrigen.TEXTO


def test_se_prefiere_la_frase_que_habla_de_solicitudes():
    # Hay dos plazos; el que importa es el de presentar.
    texto = (
        "El plazo de presentación de solicitudes finaliza el 10 de noviembre de 2026. "
        "La resolución del procedimiento se publicará antes del plazo que termina "
        "el 20 de diciembre de 2026."
    )
    fecha, _ = extraer(texto)
    assert fecha == date(2026, 11, 10)


def test_la_misma_fecha_repetida_no_es_ambigua():
    texto = (
        "El plazo de solicitudes termina el 15 de noviembre de 2026. "
        "Se recuerda que el plazo de presentación finaliza el 15 de noviembre de 2026."
    )
    assert extraer(texto)[0] == date(2026, 11, 15)


def test_una_fecha_limite_que_no_es_de_presentacion_no_se_coge():
    """Caso real que se colaba: la duración de la beca, no su plazo.

    En BOE-B-2026-33137 el texto dice que la beca "no podrá superar la fecha
    límite de 31 de diciembre de 2028", y el plazo de solicitud verdadero es
    relativo ("un mes desde la publicación"). Se extraía el 2028 como plazo.
    """
    texto = (
        "La segunda beca tendrá una duración inicial de 12 meses que no podrá superar "
        "la fecha límite de 31 de diciembre de 2028. El plazo de presentación de "
        "solicitudes será de un mes a contar desde el día siguiente al de publicación."
    )
    fecha, origen = extraer(texto)
    assert fecha is None
    assert origen is PlazoOrigen.DESCONOCIDO


def test_un_plazo_sin_mencionar_que_se_presenta_algo_no_vale():
    # Una fecha cerca de la palabra "plazo" puede ser cualquier cosa: duración,
    # vigencia, resolución. Se exige que la frase hable de presentar.
    assert extraer("El plazo finaliza el 5 de diciembre de 2026.")[0] is None


# --------------------------------------------------------------------------- #
# Lo que NO se inventa
# --------------------------------------------------------------------------- #


def test_un_plazo_relativo_en_dias_habiles_no_se_calcula():
    # Es la forma mas comun en el BOE, y calcularla exige el calendario de
    # festivos nacionales, autonomicos y locales. Equivocarse en un dia es
    # equivocarse.
    fecha, origen = extraer(
        "El plazo de presentación de solicitudes será de veinte días hábiles contados "
        "a partir del día siguiente al de la publicación de esta Resolución."
    )
    assert fecha is None
    assert origen is PlazoOrigen.DESCONOCIDO


def test_dos_plazos_distintos_de_presentacion_son_ambiguos():
    texto = (
        "El plazo de presentación de solicitudes del turno libre finaliza el "
        "10 de noviembre de 2026. El plazo de presentación de solicitudes del turno "
        "de promoción interna finaliza el 25 de noviembre de 2026."
    )
    assert extraer(texto)[0] is None


def test_una_fecha_anterior_a_la_publicacion_se_descarta():
    # No puede ser un plazo de presentacion de algo que se publica hoy.
    assert extraer("El plazo de solicitudes finalizó el 1 de enero de 2020.")[0] is None


def test_los_plazos_de_alegaciones_y_recursos_no_cuentan():
    texto = (
        "Contra esta resolución podrá interponerse recurso en el plazo que finaliza "
        "el 20 de noviembre de 2026."
    )
    assert extraer(texto)[0] is None


def test_un_texto_sin_plazos_no_devuelve_nada():
    fecha, origen = extraer("Se publica el convenio suscrito entre ambas entidades.")
    assert fecha is None
    assert origen is PlazoOrigen.DESCONOCIDO


def test_una_fecha_sin_contexto_de_plazo_no_cuenta():
    assert extraer("La Orden de 3 de octubre de 2026 regula el procedimiento.")[0] is None


def test_un_texto_vacio_no_rompe():
    assert extraer_plazo(None) == (None, PlazoOrigen.DESCONOCIDO)
    assert extraer_plazo("") == (None, PlazoOrigen.DESCONOCIDO)


def test_una_fecha_imposible_se_ignora():
    assert extraer("El plazo de solicitudes termina el 31 de febrero de 2026.")[0] is None


def test_sin_fecha_de_publicacion_no_se_filtra_por_antiguedad():
    # Util para textos sueltos: sin referencia no se descarta nada por fecha.
    fecha, _ = extraer_plazo("El plazo de solicitudes terminó el 1 de enero de 2020.")
    assert fecha == date(2020, 1, 1)


# --------------------------------------------------------------------------- #
# Contra el texto real del BOE
# --------------------------------------------------------------------------- #


def test_el_documento_real_no_declara_un_plazo_absoluto(documento_boe):
    from bs4 import BeautifulSoup

    from convocatorias_mcp.sources.boe import extraer_texto

    texto = extraer_texto(BeautifulSoup(documento_boe, "lxml-xml"))
    fecha, origen = extraer_plazo(texto, publicado=PUBLICADO)

    # Esta convocatoria usa el plazo relativo ("quince días hábiles"), asi que
    # lo honesto es no dar fecha. Si algun dia el parser la diera, este test
    # avisaria de que algo se esta inventando.
    assert fecha is None
    assert origen is PlazoOrigen.DESCONOCIDO
    assert "habiles" in texto.lower() or "hábiles" in texto.lower()
