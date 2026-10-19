"""Extracción del plazo de presentación desde el texto oficial.

Esto es deliberadamente **conservador**: solo se devuelve una fecha cuando el
texto la dice de forma explícita y sin ambigüedad. Un plazo equivocado es peor
que no tener plazo, porque alguien se fiaría de él y perdería la convocatoria.

Lo que **no** se intenta: los plazos relativos del tipo *"veinte días hábiles
contados a partir del día siguiente al de la publicación"*, que son la forma más
común en el BOE. Calcularlos exige el calendario de festivos nacionales,
autonómicos y locales del año correspondiente, y equivocarse en un día es
equivocarse. Esos casos quedan en ``PlazoOrigen.DESCONOCIDO``, y el servidor lo
dice con palabras en vez de callarse.

Cuando hay varias fechas candidatas con significados distintos (presentación,
alegaciones, resolución) tampoco se elige: se devuelve ``None``.
"""

from __future__ import annotations

import re
from datetime import date

from convocatorias_mcp.models import PlazoOrigen, strip_accents

__all__ = ["MESES", "extraer_plazo"]

MESES: dict[str, int] = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

_FECHA_LARGA = re.compile(
    r"\b(\d{1,2})\s+de\s+(" + "|".join(MESES) + r")\s+de\s+(\d{4})\b",
    re.IGNORECASE,
)
_FECHA_CORTA = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")

#: Una frase habla de un plazo si menciona una de estas.
_MARCAS_PLAZO = ("plazo", "hasta el", "finalizar", "finaliza", "termina", "limite")

#: Y es el plazo que nos interesa si además habla de presentar algo.
_MARCAS_PRESENTACION = ("solicitud", "instancia", "participacion", "inscripcion", "presentacion")

#: Frases que hablan de otros plazos y que conviene no confundir con el bueno.
_MARCAS_DESCARTE = ("alegacion", "recurso", "reclamacion", "subsanacion", "resolucion del")

_FIN_DE_FRASE = re.compile(r"(?<=[.;])\s+")


def _fechas_en(frase: str) -> list[date]:
    """Fechas absolutas de una frase, en el orden en que aparecen."""
    encontradas: list[date] = []

    for dia, mes, anio in _FECHA_LARGA.findall(frase):
        mes_numero = MESES[strip_accents(mes).lower()]
        fecha = _construir(int(anio), mes_numero, int(dia))
        if fecha is not None:
            encontradas.append(fecha)

    for dia, mes, anio in _FECHA_CORTA.findall(frase):
        fecha = _construir(int(anio), int(mes), int(dia))
        if fecha is not None:
            encontradas.append(fecha)

    return encontradas


def _construir(anio: int, mes: int, dia: int) -> date | None:
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


def extraer_plazo(
    texto: str | None, *, publicado: date | None = None
) -> tuple[date | None, PlazoOrigen]:
    """Busca el último día para presentar solicitudes.

    Devuelve la fecha y su origen, listos para asignar a
    ``Convocatoria.plazo_fin`` y ``plazo_origen``. Cuando no se puede
    determinar devuelve ``(None, PlazoOrigen.DESCONOCIDO)``, que es lo que el
    validador del modelo exige para no guardar una fecha sin procedencia.

    ``publicado`` sirve de cordura: un plazo anterior a la publicación no es un
    plazo de presentación, así que se descarta.
    """
    if not texto:
        return None, PlazoOrigen.DESCONOCIDO

    candidatas: list[date] = []

    for frase in _FIN_DE_FRASE.split(texto):
        plana = strip_accents(frase).lower()

        # Se exigen las dos cosas en la MISMA frase: que hable de un plazo y
        # que hable de presentar algo. Aceptar cualquier fecha cercana a la
        # palabra "plazo" da falsos positivos caros. Caso real que se colaba:
        # en una convocatoria de becas, "no podrá superar la fecha límite de 31
        # de diciembre de 2028" es la duración de la beca, y se guardaba como
        # plazo de solicitud cuando el verdadero era relativo ("un mes desde la
        # publicación") y por tanto no calculable.
        if not any(marca in plana for marca in _MARCAS_PLAZO):
            continue
        if not any(marca in plana for marca in _MARCAS_PRESENTACION):
            continue
        if any(marca in plana for marca in _MARCAS_DESCARTE):
            # Habla de presentar, pero alegaciones o recursos, no la solicitud.
            continue

        candidatas.extend(
            fecha for fecha in _fechas_en(frase) if publicado is None or fecha >= publicado
        )

    distintas = set(candidatas)
    if len(distintas) == 1:
        return distintas.pop(), PlazoOrigen.TEXTO

    # Sin fechas, o varias que significan cosas distintas. No se elige ninguna.
    return None, PlazoOrigen.DESCONOCIDO
