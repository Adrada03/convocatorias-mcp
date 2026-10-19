"""Tests del scoring de afinidad.

Lo que se verifica: que es barato (no mira el texto completo), que una sección
no vigilada es una exclusión y no un cero, que el orden sigue siendo
determinista y que los pesos se pueden afinar sin tocar la lógica.
"""

from __future__ import annotations

from datetime import date

from convocatorias_mcp.models import Seccion, Watchlist, WatchlistsConfig
from convocatorias_mcp.ranking import PESOS_POR_DEFECTO, Pesos, puntuar, rankear

BECAS = Watchlist(nombre="becas-informatica", keywords=["informatica"], secciones=["III"])


# --------------------------------------------------------------------------- #
# Puntuacion
# --------------------------------------------------------------------------- #


def test_una_keyword_en_el_titulo_puntua_alto(convocatoria_factory):
    convocatoria = convocatoria_factory(
        titulo="Resolución por la que se convocan becas de informática", seccion=Seccion.III
    )

    match = puntuar(convocatoria, BECAS)

    assert match is not None
    assert match.watchlist == "becas-informatica"
    assert match.keywords_encontradas == ["informatica"]
    assert match.keywords_ausentes == []
    assert match.total > 0.9


def test_una_keyword_solo_en_el_departamento_puntua_menos(convocatoria_factory):
    en_titulo = convocatoria_factory(titulo="Becas de informática", seccion=Seccion.III)
    en_departamento = convocatoria_factory(
        titulo="Resolución de concesión de becas",
        departamento="MINISTERIO DE INFORMÁTICA",
        seccion=Seccion.III,
    )

    alto = puntuar(en_titulo, BECAS)
    bajo = puntuar(en_departamento, BECAS)

    assert alto is not None and bajo is not None
    assert alto.total > bajo.total
    # Pero cuenta: la keyword aparece, aunque no en el título.
    assert bajo.keywords_encontradas == ["informatica"]


def test_sin_ninguna_keyword_el_score_baja_pero_la_seccion_suma(convocatoria_factory):
    convocatoria = convocatoria_factory(titulo="Subvenciones a la ganadería", seccion=Seccion.III)

    match = puntuar(convocatoria, BECAS)

    assert match is not None
    assert match.keywords_ausentes == ["informatica"]
    # Solo aporta la sección: 0.25 sobre los 0.80 de peso que cuentan.
    assert 0.30 < match.total < 0.35


def test_una_seccion_no_vigilada_es_una_exclusion_no_un_cero(convocatoria_factory):
    # Si alguien vigila solo III, una oposición de II.B no es "poco afín": es
    # que no la ha pedido.
    convocatoria = convocatoria_factory(titulo="Becas de informática", seccion=Seccion.II_B)
    assert puntuar(convocatoria, BECAS) is None


def test_el_min_score_corta_el_ruido(convocatoria_factory):
    convocatoria = convocatoria_factory(titulo="Subvenciones a la ganadería", seccion=Seccion.III)
    exigente = Watchlist(nombre="becas", keywords=["informatica"], secciones=["III"], min_score=0.5)

    assert puntuar(convocatoria, BECAS) is not None
    assert puntuar(convocatoria, exigente) is None


def test_una_watchlist_sin_secciones_premia_las_que_traen_convocatorias(convocatoria_factory):
    solo_keywords = Watchlist(nombre="tic", keywords=["informatica"])
    interesante = convocatoria_factory(titulo="Becas de informática", seccion=Seccion.III)
    aburrida = convocatoria_factory(titulo="Becas de informática", seccion=Seccion.IV)

    buena = puntuar(interesante, solo_keywords)
    mala = puntuar(aburrida, solo_keywords)

    assert buena is not None and mala is not None
    assert buena.total > mala.total


def test_una_watchlist_solo_de_secciones_puede_llegar_al_maximo(convocatoria_factory):
    # El total se normaliza entre las dimensiones que aplican, así que no se
    # queda con el techo del peso de la sección.
    solo_seccion = Watchlist(nombre="empleo", secciones=["II.B"])
    convocatoria = convocatoria_factory(seccion=Seccion.II_B)

    match = puntuar(convocatoria, solo_seccion)

    assert match is not None
    assert match.total == 1.0


def test_el_desglose_explica_cada_dimension(convocatoria_factory):
    convocatoria = convocatoria_factory(titulo="Becas de informática", seccion=Seccion.III)

    match = puntuar(convocatoria, BECAS)

    assert match is not None
    dimensiones = {componente.nombre for componente in match.componentes}
    assert dimensiones == {"titulo", "departamento", "seccion"}
    for componente in match.componentes:
        assert componente.detalle, componente.nombre
        assert componente.puntos == round(componente.peso * componente.ratio, 4)


def test_el_ambito_desconocido_no_penaliza(convocatoria_factory):
    con_ambito = Watchlist(nombre="madrid", keywords=["informatica"], ambito=["Madrid"])
    sin_dato = convocatoria_factory(titulo="Becas de informática", ambito=[])
    con_dato = convocatoria_factory(titulo="Becas de informática", ambito=["Madrid"])

    neutro = puntuar(sin_dato, con_ambito)
    premiado = puntuar(con_dato, con_ambito)

    assert neutro is not None and premiado is not None
    # El que lo declara y coincide puntúa más, pero el que no lo trae no se
    # castiga por lo que su fuente no dice.
    assert premiado.total >= neutro.total
    assert "ambito" not in {c.nombre for c in neutro.componentes}


def test_el_scoring_no_usa_el_texto_completo(convocatoria_factory):
    # Es el principio de los dos niveles: puntuar con el texto obligaria a
    # descargar 15.000 documentos.
    sin_texto = convocatoria_factory(titulo="Becas de informática", seccion=Seccion.III)
    con_texto = convocatoria_factory(
        titulo="Becas de informática",
        seccion=Seccion.III,
        texto_completo="informatica " * 500,
    )

    uno = puntuar(sin_texto, BECAS)
    otro = puntuar(con_texto, BECAS)

    assert uno is not None and otro is not None
    assert uno.total == otro.total


def test_los_pesos_se_pueden_afinar(convocatoria_factory):
    convocatoria = convocatoria_factory(titulo="Subvenciones a la ganadería", seccion=Seccion.III)

    por_defecto = puntuar(convocatoria, BECAS)
    con_seccion_mas_pesada = puntuar(
        convocatoria, BECAS, pesos=Pesos(titulo=0.2, seccion=0.7, departamento=0.1)
    )

    assert por_defecto is not None and con_seccion_mas_pesada is not None
    assert con_seccion_mas_pesada.total > por_defecto.total


def test_los_pesos_por_defecto_dan_mas_peso_al_titulo():
    assert PESOS_POR_DEFECTO.titulo > PESOS_POR_DEFECTO.seccion
    assert PESOS_POR_DEFECTO.seccion > PESOS_POR_DEFECTO.departamento
    assert PESOS_POR_DEFECTO.departamento > PESOS_POR_DEFECTO.ambito


# --------------------------------------------------------------------------- #
# Ordenacion
# --------------------------------------------------------------------------- #


def _config() -> WatchlistsConfig:
    return WatchlistsConfig(
        watchlists=[
            BECAS,
            Watchlist(nombre="empleo-tic", keywords=["programador"], secciones=["II.B"]),
        ]
    )


def test_rankear_ordena_por_score(convocatoria_factory):
    convocatorias = [
        convocatoria_factory(
            identificador="BOE-A-2026-1", titulo="Subvenciones varias", seccion=Seccion.III
        ),
        convocatoria_factory(
            identificador="BOE-A-2026-2", titulo="Becas de informática", seccion=Seccion.III
        ),
    ]

    ordenadas = rankear(convocatorias, _config())

    assert [c.identificador for c in ordenadas] == ["BOE-A-2026-2", "BOE-A-2026-1"]
    assert ordenadas[0].score is not None
    assert ordenadas[0].watchlists == ["becas-informatica"]


def test_rankear_sin_watchlists_no_toca_nada(convocatoria_factory):
    convocatorias = [convocatoria_factory()]

    assert rankear(convocatorias, None) == convocatorias
    assert rankear(convocatorias, WatchlistsConfig()) == convocatorias


def test_el_empate_se_desempata_con_el_orden_canonico(convocatoria_factory):
    # Dos consultas identicas tienen que devolver el mismo orden.
    convocatorias = [
        convocatoria_factory(
            identificador=f"BOE-A-2026-{indice}",
            titulo="Becas de informática",
            seccion=Seccion.III,
            fecha_publicacion=date(2026, 10, 17),
        )
        for indice in (3, 1, 2)
    ]

    primera = [c.identificador for c in rankear(convocatorias, _config())]
    segunda = [c.identificador for c in rankear(list(reversed(convocatorias)), _config())]

    assert primera == segunda == ["BOE-A-2026-1", "BOE-A-2026-2", "BOE-A-2026-3"]


def test_una_convocatoria_puede_coincidir_con_varias_watchlists(convocatoria_factory):
    config = WatchlistsConfig(
        watchlists=[
            Watchlist(nombre="una", keywords=["informatica"]),
            Watchlist(nombre="otra", keywords=["becas"]),
        ]
    )
    convocatoria = convocatoria_factory(titulo="Becas de informática", seccion=Seccion.III)

    ordenadas = rankear([convocatoria], config)

    assert sorted(ordenadas[0].watchlists) == ["otra", "una"]
    # El score expuesto es el mejor de todas.
    assert ordenadas[0].score == max(m.total for m in ordenadas[0].matches)


def test_solo_coincidentes_descarta_lo_que_no_encaja(convocatoria_factory):
    convocatorias = [
        convocatoria_factory(identificador="BOE-A-2026-1", seccion=Seccion.IV),
        convocatoria_factory(
            identificador="BOE-A-2026-2", titulo="Becas de informática", seccion=Seccion.III
        ),
    ]

    con_todas = rankear(convocatorias, _config())
    solo_las_buenas = rankear(convocatorias, _config(), solo_coincidentes=True)

    assert len(con_todas) == 2
    assert [c.identificador for c in solo_las_buenas] == ["BOE-A-2026-2"]
