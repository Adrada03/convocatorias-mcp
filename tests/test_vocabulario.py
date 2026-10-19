"""Tests del diccionario de sinónimos.

Es una red de seguridad, no el mecanismo principal: lo que de verdad traduce
"quiero oposiciones de informática" al vocabulario del BOE es la descripción de
``search_convocatorias``, que se la lee el modelo. Lo que se verifica aquí es
que la red no estorbe: que una palabra sin sinónimos se busque tal cual y que
un fichero del usuario pueda corregir una entrada, no solo añadir.
"""

from __future__ import annotations

from pathlib import Path

from convocatorias_mcp.vocabulario import (
    SINONIMOS,
    cargar_sinonimos,
    expandir_keyword,
    expandir_keywords,
)


def test_una_palabra_coloquial_se_expande_al_vocabulario_del_boletin():
    variantes = expandir_keyword("oposiciones")

    # La palabra original va primera, aunque no aparezca en ningún título.
    assert variantes[0] == "oposiciones"
    assert "proceso selectivo" in variantes
    assert "pruebas selectivas" in variantes


def test_una_palabra_sin_sinonimos_se_busca_tal_cual():
    # Es lo que permite que la tabla sea pequeña sin estorbar.
    assert expandir_keyword("informatica") == ["informatica"]


def test_la_expansion_normaliza_acentos_y_mayusculas():
    assert expandir_keyword("Oposiciones") == expandir_keyword("oposiciones")
    assert expandir_keyword("informática") == ["informatica"]


def test_una_palabra_vacia_no_genera_variantes():
    assert expandir_keyword("   ") == []
    assert expandir_keyword("¡!") == []


def test_expandir_keywords_mantiene_una_lista_por_palabra():
    # La busqueda exige TODAS las palabras pero le vale CUALQUIER variante de
    # cada una: aplanarlas perderia esa diferencia.
    grupos = expandir_keywords(["oposiciones", "informatica"])

    assert len(grupos) == 2
    assert "proceso selectivo" in grupos[0]
    assert grupos[1] == ["informatica"]


def test_expandir_keywords_descarta_las_vacias():
    assert expandir_keywords(["", "informatica"]) == [["informatica"]]


def test_las_claves_y_los_valores_de_serie_estan_normalizados():
    # Si no, no casarian nunca contra titulo_normalizado.
    from convocatorias_mcp.models import normalize_text

    for clave, variantes in SINONIMOS.items():
        assert clave == normalize_text(clave), clave
        for variante in variantes:
            assert variante == normalize_text(variante), variante


def test_la_tabla_de_serie_se_mantiene_pequena():
    # No pretende ser exhaustiva: si crece mucho, la respuesta es mejorar la
    # descripcion del tool, no inflar la tabla.
    assert len(SINONIMOS) < 40


# --------------------------------------------------------------------------- #
# Fichero del usuario
# --------------------------------------------------------------------------- #


def test_sin_fichero_se_usan_los_de_serie():
    assert cargar_sinonimos(None) == dict(SINONIMOS)


def test_un_fichero_que_no_existe_tampoco_molesta(tmp_path: Path):
    assert cargar_sinonimos(tmp_path / "no-existe.yaml") == dict(SINONIMOS)


def test_el_fichero_del_usuario_puede_anadir_terminos(tmp_path: Path):
    ruta = tmp_path / "sinonimos.yaml"
    ruta.write_text("vivienda: [vivienda protegida, vpo]\n", encoding="utf-8")

    tabla = cargar_sinonimos(ruta)

    assert tabla["vivienda"] == ("vivienda protegida", "vpo")
    # Y los de serie siguen ahí.
    assert "oposiciones" in tabla


def test_el_fichero_del_usuario_puede_corregir_una_entrada(tmp_path: Path):
    # Sustituye en vez de sumarse, para poder arreglar una entrada que no guste.
    ruta = tmp_path / "sinonimos.yaml"
    ruta.write_text("oposiciones: [concurso de traslados]\n", encoding="utf-8")

    tabla = cargar_sinonimos(ruta)

    assert tabla["oposiciones"] == ("concurso de traslados",)


def test_un_valor_suelto_vale_como_lista(tmp_path: Path):
    ruta = tmp_path / "sinonimos.yaml"
    ruta.write_text("vivienda: vivienda protegida\n", encoding="utf-8")
    assert cargar_sinonimos(ruta)["vivienda"] == ("vivienda protegida",)


def test_el_fichero_del_usuario_se_normaliza(tmp_path: Path):
    ruta = tmp_path / "sinonimos.yaml"
    ruta.write_text("Oposición: [Proceso Selectivo]\n", encoding="utf-8")

    tabla = cargar_sinonimos(ruta)

    assert tabla["oposicion"] == ("proceso selectivo",)


def test_un_fichero_que_no_es_un_mapa_se_ignora(tmp_path: Path):
    ruta = tmp_path / "sinonimos.yaml"
    ruta.write_text("- una lista\n- en vez de un mapa\n", encoding="utf-8")
    assert cargar_sinonimos(ruta) == dict(SINONIMOS)


def test_una_entrada_vacia_no_borra_la_de_serie(tmp_path: Path):
    ruta = tmp_path / "sinonimos.yaml"
    ruta.write_text("oposiciones: []\n", encoding="utf-8")
    assert "proceso selectivo" in cargar_sinonimos(ruta)["oposiciones"]


def test_el_ejemplo_versionado_carga(tmp_path: Path):
    ruta = Path(__file__).resolve().parent.parent / "sinonimos.example.yaml"
    tabla = cargar_sinonimos(ruta)
    assert len(tabla) >= len(SINONIMOS)
