"""Puntuación de afinidad entre una convocatoria y una watchlist.

Es el nivel 1 del matching: **barato y determinista**. Puntúa con lo que trae el
sumario —título, sección, departamento y epígrafe— y nunca con el texto
completo. Puntuar con el texto obligaría a descargar 15.000 documentos y
rompería el reparto en dos niveles: este score solo ordena candidatas, y la
profundidad la pone el modelo sobre las pocas que suben.

Lo que mide es **afinidad temática, no elegibilidad**. Que una beca puntúe alto
no dice nada sobre si el usuario cumple sus requisitos de renta, expediente o
residencia: eso está en el texto oficial y lo lee el modelo.

Los pesos viven en :class:`Pesos` para poder afinarlos con el uso real sin tocar
la lógica.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from convocatorias_mcp.models import (
    Convocatoria,
    MatchComponent,
    MatchScore,
    Watchlist,
    WatchlistsConfig,
    normalize_text,
)
from convocatorias_mcp.vocabulario import expandir_keyword

__all__ = ["PESOS_POR_DEFECTO", "Pesos", "puntuar", "rankear"]


@dataclass(frozen=True)
class Pesos:
    """Cuánto aporta cada dimensión.

    No tienen que sumar uno: el total se normaliza entre las dimensiones que
    **apliquen** a cada watchlist. Así una watchlist que solo declara secciones
    puede llegar a 1.0, en vez de quedarse con el techo del peso de la sección.
    """

    #: Palabras clave en el título. Es la señal más directa de de qué va.
    titulo: float = 0.55
    #: Sección del boletín. Casi tan informativa como el título, y más barata:
    #: II.B es empleo público y III son bases de becas, sin ambigüedad.
    seccion: float = 0.25
    #: Palabras clave en el departamento o el epígrafe. Una beca del Ministerio
    #: de Ciencia tiene otro color que una de Defensa.
    departamento: float = 0.15
    #: Ámbito territorial. Pesa poco a propósito: el BOE casi nunca lo publica
    #: de forma estructurada, así que hoy esta dimensión rara vez aplica.
    ambito: float = 0.05


PESOS_POR_DEFECTO = Pesos()

#: Dimensiones que suman cuando aciertan y no restan cuando no.
_DIMENSIONES_EXTRA = frozenset({"departamento", "ambito"})


def puntuar(
    convocatoria: Convocatoria,
    watchlist: Watchlist,
    *,
    pesos: Pesos = PESOS_POR_DEFECTO,
    sinonimos: Mapping[str, tuple[str, ...]] | None = None,
) -> MatchScore | None:
    """Puntúa una convocatoria contra una watchlist.

    Devuelve ``None`` cuando la watchlist **excluye** la convocatoria, que no es
    lo mismo que puntuarla con un cero: si alguien vigila solo II.B y III, una
    licitación de V.A no es "poco afín", es que no la ha pedido.

    También devuelve ``None`` si el score no llega al ``min_score`` de la
    watchlist, que es el mando que tiene el usuario para cortar el ruido.
    """
    # Filtro duro: la sección es una elección explícita, no una preferencia.
    if not watchlist.acepta_seccion(convocatoria.seccion):
        return None

    componentes: list[MatchComponent] = []
    encontradas: list[str] = []
    ausentes: list[str] = []

    titulo = normalize_text(convocatoria.titulo)
    contexto = normalize_text(
        " ".join(
            parte
            for parte in (
                convocatoria.departamento or "",
                convocatoria.epigrafe or "",
                convocatoria.seccion_nombre or "",
            )
            if parte
        )
    )

    if watchlist.keywords:
        # Se busca con las mismas variantes que usa el filtro SQL: si una
        # convocatoria entra en los resultados porque su título dice "proceso
        # selectivo" cuando la keyword es "oposiciones", el scoring tiene que
        # verlo también. Si no, el filtro la encuentra y el ranking la descarta.
        en_titulo: list[str] = []
        en_contexto: list[str] = []
        for palabra in watchlist.keywords:
            variantes = expandir_keyword(palabra, sinonimos)
            if any(variante in titulo for variante in variantes):
                en_titulo.append(palabra)
            elif any(variante in contexto for variante in variantes):
                en_contexto.append(palabra)

        encontradas = en_titulo + en_contexto
        ausentes = [palabra for palabra in watchlist.keywords if palabra not in encontradas]

        # Filtro duro: si la watchlist declara palabras clave, al menos una
        # tiene que aparecer. La sección sigue filtrando y sumando al score,
        # pero no basta por sí sola: una watchlist que vigila II.B con tres
        # keywords no está pidiendo las 2.255 convocatorias de la sección.
        #
        # Es una regla y no un umbral numérico a propósito. Un min_score
        # colocado "justo por encima de solo-sección" solo funcionaría con los
        # pesos actuales y dejaría de hacerlo en silencio al cambiarlos.
        if not encontradas:
            return None

        total = len(watchlist.keywords)
        componentes.append(
            _componente(
                "titulo",
                pesos.titulo,
                len(en_titulo) / total,
                _detalle_keywords("el título", en_titulo, total),
            )
        )
        componentes.append(
            _componente(
                "departamento",
                pesos.departamento,
                len(en_contexto) / total,
                _detalle_keywords("el departamento o el epígrafe", en_contexto, total),
            )
        )

    if watchlist.secciones:
        componentes.append(
            _componente(
                "seccion",
                pesos.seccion,
                1.0,
                f"La sección {convocatoria.seccion.value} está entre las que vigila "
                f"esta watchlist ({', '.join(s.value for s in watchlist.secciones)}).",
            )
        )
    elif convocatoria.seccion.es_de_interes:
        # Sin secciones declaradas se premia que al menos sea una de las que
        # traen convocatorias, para que una watchlist de solo keywords no
        # ponga a la misma altura una beca y un nombramiento.
        componentes.append(
            _componente(
                "seccion",
                pesos.seccion,
                1.0,
                f"La sección {convocatoria.seccion.value} "
                f"({convocatoria.seccion.descripcion}) suele traer convocatorias.",
            )
        )
    else:
        componentes.append(
            _componente(
                "seccion",
                pesos.seccion,
                0.0,
                f"La sección {convocatoria.seccion.value} "
                f"({convocatoria.seccion.descripcion}) rara vez trae convocatorias.",
            )
        )

    if watchlist.ambito and convocatoria.ambito:
        pedidos = {normalize_text(item) for item in watchlist.ambito}
        tiene = {normalize_text(item) for item in convocatoria.ambito}
        coincide = bool(pedidos & tiene)
        componentes.append(
            _componente(
                "ambito",
                pesos.ambito,
                1.0 if coincide else 0.0,
                (
                    f"Ámbito {', '.join(sorted(tiene))}, entre los que pide la watchlist."
                    if coincide
                    else f"Ámbito {', '.join(sorted(tiene))}, que la watchlist no pide."
                ),
            )
        )
    # Si el boletín no publica el ámbito no se penaliza: sería castigar a la
    # convocatoria por lo que no dice su fuente.

    # El departamento y el ámbito son **extras**: suman cuando aciertan y no
    # restan cuando no. Si contaran siempre en el denominador, una convocatoria
    # con la keyword en el título no podría pasar de 0.84 solo porque su
    # departamento no la repite, y eso no es menos afín: es lo normal.
    total_relevante = sum(
        componente.peso
        for componente in componentes
        if componente.nombre not in _DIMENSIONES_EXTRA or componente.ratio > 0
    )
    if total_relevante <= 0:
        return None
    total = sum(componente.puntos for componente in componentes) / total_relevante

    if total < watchlist.min_score:
        return None

    return MatchScore(
        watchlist=watchlist.nombre,
        total=round(min(max(total, 0.0), 1.0), 4),
        componentes=componentes,
        keywords_encontradas=encontradas,
        keywords_ausentes=ausentes,
    )


def _componente(nombre: str, peso: float, ratio: float, detalle: str) -> MatchComponent:
    ratio = min(max(ratio, 0.0), 1.0)
    return MatchComponent(
        nombre=nombre,
        peso=peso,
        ratio=round(ratio, 4),
        puntos=round(peso * ratio, 4),
        detalle=detalle,
    )


def _detalle_keywords(donde: str, encontradas: list[str], total: int) -> str:
    if not encontradas:
        return f"Ninguna de las {total} palabras clave aparece en {donde}."
    return f"{len(encontradas)} de {total} palabras clave en {donde}: {', '.join(encontradas)}."


def rankear(
    convocatorias: list[Convocatoria],
    config: WatchlistsConfig | None,
    *,
    pesos: Pesos = PESOS_POR_DEFECTO,
    sinonimos: Mapping[str, tuple[str, ...]] | None = None,
    solo_coincidentes: bool = False,
) -> list[Convocatoria]:
    """Puntúa una lista contra todas las watchlists y la ordena.

    Sin watchlists devuelve la lista tal cual, en el orden canónico por fecha
    que ya trae: el proyecto funciona sin configurar nada.

    Con watchlists ordena por score descendente y, a igualdad, mantiene el orden
    canónico, que es determinista. ``solo_coincidentes`` descarta las que no ha
    hecho coincidir ninguna.
    """
    if config is None or config.is_empty:
        return list(convocatorias)

    puntuadas: list[Convocatoria] = []
    for convocatoria in convocatorias:
        matches = [
            match
            for match in (
                puntuar(convocatoria, watchlist, pesos=pesos, sinonimos=sinonimos)
                for watchlist in config.watchlists
            )
            if match is not None
        ]
        if not matches and solo_coincidentes:
            continue
        matches.sort(key=lambda match: (-match.total, match.watchlist))
        puntuadas.append(convocatoria.model_copy(update={"matches": matches}))

    # El desempate es la clave de orden canónico, así que dos consultas
    # idénticas siguen devolviendo el mismo orden.
    puntuadas.sort(key=lambda c: (-(c.score or 0.0), c.clave_orden))
    return puntuadas
