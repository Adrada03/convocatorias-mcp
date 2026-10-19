"""Sinónimos del vocabulario administrativo, como red de seguridad.

Los títulos del BOE están escritos en lenguaje administrativo. Una oposición no
se titula "oposiciones": se titula *"Resolución de 8 de octubre de 2026, de la
Subsecretaría, por la que se convoca proceso selectivo para ingreso..."*. Una
búsqueda por "oposiciones" sobre 2.255 convocatorias de la sección II.B devuelve
cero resultados, y es correcto: esa palabra no está en ningún título.

**La solución principal no está aquí**, sino en la descripción de
``search_convocatorias``: el cliente es un modelo, y traducir "quiero
oposiciones de informática" a ``seccion="II.B"`` más palabras del vocabulario
real del boletín es precisamente su trabajo. Este diccionario es la red debajo:
cubre los términos coloquiales más obvios para que una consulta ingenua no
devuelva cero.

No pretende ser exhaustivo, y no debería crecer mucho. Si hace falta algo más
elaborado, la respuesta es mejorar la descripción del tool, no inflar la tabla.

Se puede sustituir o ampliar con un ``sinonimos.yaml`` propio; ver
``sinonimos.example.yaml`` y la variable ``CONVOCATORIAS_SINONIMOS_PATH``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from convocatorias_mcp.models import normalize_text

__all__ = ["SINONIMOS", "cargar_sinonimos", "expandir_keyword", "expandir_keywords"]

#: Término coloquial -> términos que de verdad aparecen en los títulos.
#:
#: Las claves y los valores van normalizados (minúsculas y sin acentos) porque
#: así es como se compara contra ``titulo_normalizado``.
SINONIMOS: dict[str, tuple[str, ...]] = {
    "oposicion": ("proceso selectivo", "pruebas selectivas", "concurso oposicion", "oposicion"),
    "oposiciones": ("proceso selectivo", "pruebas selectivas", "concurso oposicion"),
    "opositor": ("proceso selectivo", "pruebas selectivas"),
    "beca": ("beca", "ayuda", "subvencion"),
    "becas": ("beca", "ayuda", "subvencion"),
    "ayudas": ("ayuda", "subvencion"),
    "subvenciones": ("subvencion", "ayuda", "bases reguladoras"),
    "plazas": ("plaza", "puesto de trabajo", "proceso selectivo"),
    "empleo": ("proceso selectivo", "puesto de trabajo", "plaza"),
    "trabajo": ("puesto de trabajo", "proceso selectivo"),
    "contratos": ("licitacion", "contratacion", "formalizacion"),
    "licitaciones": ("licitacion", "contratacion"),
    "practicas": ("practicas", "formacion"),
    "funcionario": ("funcionarios", "proceso selectivo", "cuerpo"),
    "interino": ("interinidad", "personal interino"),
}


def expandir_keyword(
    palabra: str, sinonimos: Mapping[str, tuple[str, ...]] | None = None
) -> list[str]:
    """Variantes con las que buscar una palabra, empezando por ella misma.

    Si no tiene sinónimos conocidos se devuelve sola, que es lo que hace que
    este diccionario pueda ser pequeño sin estorbar.
    """
    tabla = SINONIMOS if sinonimos is None else sinonimos
    normalizada = normalize_text(palabra)
    if not normalizada:
        return []

    variantes = [normalizada]
    for variante in tabla.get(normalizada, ()):
        normalizada_variante = normalize_text(variante)
        if normalizada_variante and normalizada_variante not in variantes:
            variantes.append(normalizada_variante)
    return variantes


def expandir_keywords(
    palabras: list[str], sinonimos: Mapping[str, tuple[str, ...]] | None = None
) -> list[list[str]]:
    """Expande cada palabra a sus variantes.

    Devuelve una lista por palabra, porque la búsqueda exige **todas** las
    palabras (AND) pero le basta **cualquiera** de las variantes de cada una
    (OR). Mezclarlas en una sola lista perdería esa diferencia.
    """
    expandidas = [expandir_keyword(palabra, sinonimos) for palabra in palabras]
    return [variantes for variantes in expandidas if variantes]


def cargar_sinonimos(path: Path | None) -> dict[str, tuple[str, ...]]:
    """Carga un ``sinonimos.yaml`` del usuario encima de los de serie.

    Sin fichero se devuelven los de serie. Una clave del fichero **sustituye**
    la de serie en lugar de sumarse, para que se pueda corregir una entrada que
    no guste y no solo añadir.
    """
    tabla = dict(SINONIMOS)
    if path is None or not path.is_file():
        return tabla

    crudo = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(crudo, dict):
        return tabla

    for clave, valor in crudo.items():
        variantes = [valor] if isinstance(valor, str) else list(valor or [])
        normalizadas = tuple(
            normalize_text(str(item)) for item in variantes if normalize_text(str(item))
        )
        if normalizadas:
            tabla[normalize_text(str(clave))] = normalizadas
    return tabla
