"""Tests de los modelos de dominio.

Se centran en lo que el resto del proyecto da por supuesto y en las dos reglas
que atraviesan el modelo: que los códigos de sección del BOE se traducen bien
en los dos sentidos, y que **nada se inventa** — un plazo sin origen conocido no
se puede guardar, y un plazo desconocido devuelve ``None``, no un cero.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from convocatorias_mcp.models import (
    AVISO_NO_ELEGIBILIDAD,
    Convocatoria,
    Estado,
    EstadoIngesta,
    MatchScore,
    PlazoOrigen,
    SearchFilters,
    Seccion,
    Seguimiento,
    SourceHealth,
    Watchlist,
    WatchlistsConfig,
    normalize_text,
)

# --------------------------------------------------------------------------- #
# Secciones: la trampa de los codigos
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("codigo_api", "esperada"),
    [
        ("1", Seccion.I),
        ("2A", Seccion.II_A),
        ("2B", Seccion.II_B),
        ("3", Seccion.III),
        ("4", Seccion.IV),
        ("5A", Seccion.V_A),
        ("5B", Seccion.V_B),
        ("5C", Seccion.V_C),
        ("99", Seccion.DESCONOCIDA),
        (None, Seccion.DESCONOCIDA),
    ],
)
def test_traduce_el_codigo_del_sumario_a_seccion(codigo_api, esperada):
    # El sumario del BOE usa "2B", no "II.B": verificado contra la API real.
    assert Seccion.from_api_code(codigo_api) is esperada


def test_la_traduccion_de_secciones_es_reversible():
    for seccion in Seccion:
        if seccion is Seccion.DESCONOCIDA:
            continue
        assert Seccion.from_api_code(seccion.codigo_api) is seccion


@pytest.mark.parametrize(
    ("escrito", "esperada"),
    [
        ("II.B", Seccion.II_B),
        ("ii.b", Seccion.II_B),
        ("IIB", Seccion.II_B),
        ("2B", Seccion.II_B),
        ("III", Seccion.III),
        ("3", Seccion.III),
        (" v.b ", Seccion.V_B),
        ("inventada", Seccion.DESCONOCIDA),
    ],
)
def test_from_text_acepta_las_dos_formas(escrito, esperada):
    # El usuario escribe "III" en su YAML; la API dice "3". Las dos valen.
    assert Seccion.from_text(escrito) is esperada


def test_las_secciones_de_interes_son_las_que_traen_convocatorias():
    assert Seccion.II_B.es_de_interes
    assert Seccion.III.es_de_interes
    assert Seccion.V_A.es_de_interes
    assert Seccion.V_B.es_de_interes
    assert not Seccion.I.es_de_interes
    assert not Seccion.IV.es_de_interes


def test_cada_seccion_tiene_descripcion_legible():
    assert "Oposiciones" in Seccion.II_B.descripcion
    assert Seccion.DESCONOCIDA.descripcion == "Sección desconocida"


# --------------------------------------------------------------------------- #
# Convocatoria
# --------------------------------------------------------------------------- #


def test_el_id_es_estable_y_legible(convocatoria_factory):
    convocatoria = convocatoria_factory()
    assert convocatoria.id == "boe:BOE-A-2026-21635"
    assert convocatoria_factory().id == convocatoria.id


def test_el_id_lleva_prefijo_de_fuente_para_no_colisionar(convocatoria_factory):
    boe = convocatoria_factory(fuente="boe")
    bocm = convocatoria_factory(fuente="bocm", identificador="BOCM-A-2026-21635")
    assert boe.id != bocm.id


@pytest.mark.parametrize(
    "identificador",
    ["BOE-A-2026-21635", "BOE-B-2026-33841", "BOE-S-2026-250", "BOCM-A-2026-1"],
)
def test_acepta_identificadores_oficiales(convocatoria_factory, identificador):
    assert convocatoria_factory(identificador=identificador).identificador == identificador


@pytest.mark.parametrize(
    "identificador", ["21635", "BOE-2026-21635", "BOE-A-26-1", "", "BOE-A-2026-"]
)
def test_rechaza_identificadores_que_no_son_oficiales(convocatoria_factory, identificador):
    with pytest.raises(ValidationError, match="identificador"):
        convocatoria_factory(identificador=identificador)


def test_el_identificador_se_normaliza_a_mayusculas(convocatoria_factory):
    assert (
        convocatoria_factory(identificador="boe-a-2026-21635").identificador == "BOE-A-2026-21635"
    )


def test_las_urls_deben_ser_absolutas(convocatoria_factory):
    with pytest.raises(ValidationError, match="absolutas"):
        convocatoria_factory(url_html="/diario_boe/txt.php?id=BOE-A-2026-21635")


def test_el_pdf_es_opcional(convocatoria_factory):
    assert convocatoria_factory(url_pdf=None).url_pdf is None


def test_los_campos_vacios_quedan_en_none(convocatoria_factory):
    # Hay items del sumario sin epigrafe y sin numero de expediente.
    convocatoria = convocatoria_factory(epigrafe="   ", control=None, departamento=None)
    assert convocatoria.epigrafe is None
    assert convocatoria.control is None
    assert convocatoria.departamento is None


def test_round_trip_json_preserva_la_convocatoria(convocatoria_factory):
    original = convocatoria_factory(
        plazo_fin=date(2026, 11, 5), plazo_origen=PlazoOrigen.TEXTO, ambito=["Estatal"]
    )
    recuperada = Convocatoria.model_validate_json(original.model_dump_json())
    assert recuperada.id == original.id
    assert recuperada.fecha_publicacion == original.fecha_publicacion
    assert recuperada.plazo_fin == original.plazo_fin
    assert recuperada.ambito == original.ambito


def test_el_ambito_se_limpia_y_deduplica(convocatoria_factory):
    convocatoria = convocatoria_factory(ambito=["Estatal", " estatal ", "", "Madrid"])
    assert convocatoria.ambito == ["Estatal", "Madrid"]


def test_texto_para_matching_junta_lo_disponible(convocatoria_factory):
    texto = convocatoria_factory(departamento="MINISTERIO DE EDUCACIÓN").texto_para_matching
    assert "ministerio de educacion" in texto
    assert "resolucion" in texto


def test_texto_para_matching_funciona_sin_texto_completo(convocatoria_factory):
    # El filtro de nivel 1 tiene que servir con solo lo que trae el sumario.
    convocatoria = convocatoria_factory(texto_completo=None)
    assert convocatoria.texto_para_matching
    assert not convocatoria.texto_descargado


def test_sabe_si_ya_tiene_el_texto_descargado(convocatoria_factory):
    assert convocatoria_factory(texto_completo="De acuerdo con...").texto_descargado


# --------------------------------------------------------------------------- #
# Plazos: nada se inventa
# --------------------------------------------------------------------------- #


def test_un_plazo_sin_origen_conocido_no_se_puede_guardar(convocatoria_factory):
    # La salvaguarda del "nada se inventa": si hay fecha, salio de algun sitio.
    with pytest.raises(ValidationError, match="de dónde salió"):
        convocatoria_factory(plazo_fin=date(2026, 11, 5))


def test_un_origen_de_texto_sin_fecha_es_incoherente(convocatoria_factory):
    with pytest.raises(ValidationError, match="no hay plazo_fin"):
        convocatoria_factory(plazo_fin=None, plazo_origen=PlazoOrigen.TEXTO)


def test_sin_plazo_el_origen_es_desconocido(convocatoria_factory):
    convocatoria = convocatoria_factory()
    assert convocatoria.plazo_fin is None
    assert convocatoria.plazo_desconocido
    assert convocatoria.dias_hasta_plazo(date(2026, 10, 19)) is None


def test_dias_hasta_plazo_cuenta_bien(convocatoria_factory):
    convocatoria = convocatoria_factory(
        plazo_fin=date(2026, 10, 22), plazo_origen=PlazoOrigen.TEXTO
    )
    assert convocatoria.dias_hasta_plazo(date(2026, 10, 19)) == 3
    assert convocatoria.dias_hasta_plazo(date(2026, 10, 25)) == -3


def test_vence_en_solo_cuenta_plazos_abiertos(convocatoria_factory):
    hoy = date(2026, 10, 19)
    pronto = convocatoria_factory(plazo_fin=date(2026, 10, 21), plazo_origen=PlazoOrigen.TEXTO)
    lejano = convocatoria_factory(plazo_fin=date(2026, 12, 1), plazo_origen=PlazoOrigen.TEXTO)
    vencido = convocatoria_factory(plazo_fin=date(2026, 10, 1), plazo_origen=PlazoOrigen.TEXTO)
    desconocido = convocatoria_factory()

    assert pronto.vence_en(3, hoy=hoy)
    assert not lejano.vence_en(3, hoy=hoy)
    assert not vencido.vence_en(3, hoy=hoy)
    # Un plazo desconocido no se cuenta como "vence pronto": seria inventarselo.
    assert not desconocido.vence_en(3, hoy=hoy)


def test_se_puede_declarar_que_no_hay_plazo(convocatoria_factory):
    convocatoria = convocatoria_factory(plazo_origen=PlazoOrigen.SIN_PLAZO)
    assert convocatoria.plazo_fin is None
    assert not convocatoria.plazo_desconocido


# --------------------------------------------------------------------------- #
# Coincidencias con watchlists
# --------------------------------------------------------------------------- #


def test_sin_coincidencias_no_hay_score(convocatoria_factory):
    convocatoria = convocatoria_factory()
    assert convocatoria.watchlists == []
    assert convocatoria.score is None


def test_el_score_es_el_mejor_de_sus_watchlists(convocatoria_factory):
    convocatoria = convocatoria_factory(
        matches=[
            MatchScore(watchlist="becas-informatica", total=0.4),
            MatchScore(watchlist="empleo-publico-tic", total=0.9),
        ]
    )
    assert convocatoria.watchlists == ["becas-informatica", "empleo-publico-tic"]
    assert convocatoria.score == 0.9


def test_el_score_esta_acotado_entre_cero_y_uno():
    with pytest.raises(ValidationError):
        MatchScore(watchlist="x", total=1.5)


def test_el_estado_por_defecto_es_nueva(convocatoria_factory):
    assert convocatoria_factory().estado is Estado.NUEVA


# --------------------------------------------------------------------------- #
# Seguimiento
# --------------------------------------------------------------------------- #


def test_solo_una_descartada_lleva_motivo():
    with pytest.raises(ValidationError, match="descartada"):
        Seguimiento(
            convocatoria_id="boe:BOE-A-2026-1", estado=Estado.SOLICITADA, motivo="no me interesa"
        )


def test_una_descartada_registra_el_motivo():
    seguimiento = Seguimiento(
        convocatoria_id="boe:BOE-A-2026-1", estado=Estado.DESCARTADA, motivo="pide experiencia"
    )
    assert seguimiento.motivo == "pide experiencia"


def test_una_solicitada_registra_fecha_y_notas():
    seguimiento = Seguimiento(
        convocatoria_id="boe:BOE-A-2026-1",
        estado=Estado.SOLICITADA,
        fecha_solicitud=date(2026, 10, 20),
        notas="Enviado por sede electrónica",
    )
    assert seguimiento.fecha_solicitud == date(2026, 10, 20)
    assert seguimiento.notas


# --------------------------------------------------------------------------- #
# Watchlists
# --------------------------------------------------------------------------- #


def test_una_watchlist_normaliza_sus_keywords():
    watchlist = Watchlist(nombre="becas", keywords=["Informática", "informatica", "Software"])
    assert watchlist.keywords == ["informatica", "software"]


def test_una_watchlist_acepta_las_secciones_escritas_a_mano():
    watchlist = Watchlist(nombre="becas", secciones=["III", "V.B"])
    assert watchlist.secciones == [Seccion.III, Seccion.V_B]


def test_una_watchlist_acepta_los_codigos_de_la_api():
    assert Watchlist(nombre="x", secciones=["3", "5B"]).secciones == [Seccion.III, Seccion.V_B]


def test_una_seccion_mal_escrita_avisa_con_las_validas():
    with pytest.raises(ValidationError, match=r"II\.B"):
        Watchlist(nombre="x", secciones=["XIV"])


def test_una_keyword_suelta_no_tiene_que_ser_una_lista():
    # Despiste habitual en YAML: "keywords: informatica" sin corchetes.
    assert Watchlist(nombre="x", keywords="informática").keywords == ["informatica"]


def test_sin_secciones_se_vigilan_todas():
    watchlist = Watchlist(nombre="x")
    assert watchlist.vigila_todas_las_secciones
    assert watchlist.acepta_seccion(Seccion.I)


def test_con_secciones_solo_se_vigilan_esas():
    watchlist = Watchlist(nombre="x", secciones=["II.B"])
    assert watchlist.acepta_seccion(Seccion.II_B)
    assert not watchlist.acepta_seccion(Seccion.III)


def test_una_watchlist_rechaza_claves_desconocidas():
    with pytest.raises(ValidationError):
        Watchlist(nombre="x", keywordss=["python"])  # type: ignore[call-arg]


def test_el_min_score_esta_acotado():
    with pytest.raises(ValidationError):
        Watchlist(nombre="x", min_score=1.5)


# --------------------------------------------------------------------------- #
# WatchlistsConfig
# --------------------------------------------------------------------------- #


def test_la_config_rechaza_watchlists_con_el_mismo_nombre():
    with pytest.raises(ValidationError, match="dos watchlists"):
        WatchlistsConfig(
            watchlists=[{"nombre": "Becas"}, {"nombre": "becas"}]  # type: ignore[list-item]
        )


def test_la_config_busca_por_nombre_sin_distinguir_acentos():
    config = WatchlistsConfig(watchlists=[{"nombre": "becas-informática"}])  # type: ignore[list-item]
    assert config.get("BECAS-INFORMATICA") is not None
    assert config.get("otra") is None


def test_una_config_vacia_es_valida_y_se_reconoce():
    config = WatchlistsConfig()
    assert config.is_empty
    assert config.notificacion.avisar_plazo_dias == 3


# --------------------------------------------------------------------------- #
# SearchFilters
# --------------------------------------------------------------------------- #


def test_los_filtros_pueden_ir_todos_vacios():
    # "que ha salido hoy" es una pregunta valida.
    filtros = SearchFilters()
    assert filtros.keyword_list == []
    assert filtros.limite == 50


def test_keyword_list_normaliza_y_separa():
    assert SearchFilters(keywords="  Becas   Informática ").keyword_list == ["becas", "informatica"]


def test_un_rango_de_fechas_al_reves_avisa():
    with pytest.raises(ValidationError, match="posterior"):
        SearchFilters(desde=date(2026, 10, 20), hasta=date(2026, 10, 1))


def test_los_filtros_rechazan_parametros_desconocidos():
    with pytest.raises(ValidationError):
        SearchFilters(seccion_codigo="2B")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# SourceHealth
# --------------------------------------------------------------------------- #


def test_un_dia_sin_publicacion_no_es_un_problema():
    salud = SourceHealth(
        fuente="boe", fecha=date(2026, 10, 18), estado=EstadoIngesta.SIN_PUBLICACION
    )
    assert not salud.es_problema
    assert salud.items == 0


def test_un_error_si_es_un_problema_y_tiene_que_explicarse():
    salud = SourceHealth(
        fuente="boe", fecha=date(2026, 10, 19), estado=EstadoIngesta.ERROR, detalle="timeout"
    )
    assert salud.es_problema

    with pytest.raises(ValidationError, match="explicar"):
        SourceHealth(fuente="boe", fecha=date(2026, 10, 19), estado=EstadoIngesta.ERROR)


def test_un_dia_sin_publicacion_no_puede_traer_items():
    with pytest.raises(ValidationError, match="sin publicación"):
        SourceHealth(
            fuente="boe",
            fecha=date(2026, 10, 18),
            estado=EstadoIngesta.SIN_PUBLICACION,
            items=3,
        )


def test_un_dia_correcto_no_es_un_problema():
    salud = SourceHealth(fuente="boe", fecha=date(2026, 10, 17), estado=EstadoIngesta.OK, items=270)
    assert not salud.es_problema


# --------------------------------------------------------------------------- #
# Reglas de producto
# --------------------------------------------------------------------------- #


def test_el_aviso_de_no_elegibilidad_dice_lo_que_tiene_que_decir():
    # Es la regla innegociable del proyecto: vive en el modelo para que no se
    # pueda omitir al escribir la descripcion de un tool.
    assert "no determina si cumples" in AVISO_NO_ELEGIBILIDAD
    assert "texto oficial" in AVISO_NO_ELEGIBILIDAD


def test_normalize_text_quita_acentos_y_puntuacion():
    assert normalize_text("  Ingeniería  Informática, ¡becas!  ") == "ingenieria informatica becas"
