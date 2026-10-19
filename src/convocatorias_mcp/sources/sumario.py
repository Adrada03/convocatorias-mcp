"""Parseo del sumario del BOE, sin red.

Función pura: recibe el JSON ya decodificado y devuelve convocatorias. La usan
tanto :class:`convocatorias_mcp.sources.fixture.FixtureSource`, que lee un
sumario guardado, como la fuente real que lo descarga, así que el mapeo está
escrito y testeado una sola vez.

La estructura, verificada contra la API real:

    data.sumario.diario[].seccion[].departamento[].epigrafe[].item[]

con dos detalles que el JSON de verdad trae y la documentación no deja claros:

* **Cualquier nivel puede venir como objeto en vez de lista** cuando hay un solo
  elemento, así que todo se normaliza a lista antes de recorrerlo.
* **``epigrafe`` puede no existir**: en la sección V.B hay departamentos con
  ``item`` directamente debajo.
* **``url_pdf`` es un objeto**, con la URL dentro de su clave ``texto``, no una
  cadena.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from convocatorias_mcp.models import Convocatoria, Seccion

__all__ = ["ParseoSumarioError", "fecha_del_sumario", "parsear_sumario"]


class ParseoSumarioError(Exception):
    """El JSON no tiene la forma de un sumario."""


def _como_lista(valor: Any) -> list[Any]:
    """Normaliza a lista: la API usa objeto suelto cuando hay un solo elemento."""
    if valor is None:
        return []
    if isinstance(valor, list):
        return valor
    return [valor]


def _hijos(nodo: Any, clave: str) -> list[Any]:
    """Hijos de un nodo, con o sin el envoltorio ``texto``.

    Algunos sumarios meten un nivel ``texto`` entre un nodo y sus hijos:
    ``seccion.texto.departamento`` en vez de ``seccion.departamento``, y
    ``departamento.texto.epigrafe`` en vez de ``departamento.epigrafe``.

    Lo descubrí en el backfill real: los domingos 26/07/2026 y 09/08/2026 el
    BOE sí publicó (ediciones 181 y 194, por emergencias de incendios y
    controles fronterizos) y devolvió 200 con esta forma. El parser no veía
    ningún item y el canario de "sumario vacío" lo cazó.
    """
    if not isinstance(nodo, dict):
        return []
    directos = _como_lista(nodo.get(clave))
    if directos:
        return directos
    envoltorio = nodo.get("texto")
    if isinstance(envoltorio, dict):
        return _como_lista(envoltorio.get(clave))
    return []


def _sumario_de(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ParseoSumarioError(f"se esperaba un objeto JSON, no {type(payload).__name__}")
    sumario = (payload.get("data") or {}).get("sumario")
    if not isinstance(sumario, dict):
        raise ParseoSumarioError("el JSON no contiene data.sumario")
    return sumario


def fecha_del_sumario(payload: Any) -> date | None:
    """Fecha que declara el propio sumario, en ``metadatos.fecha_publicacion``.

    Llega como ``"20261017"``: sin hora, que es la razón de que
    ``Convocatoria.fecha_publicacion`` sea un ``date``.
    """
    metadatos = _sumario_de(payload).get("metadatos") or {}
    crudo = metadatos.get("fecha_publicacion")
    if not crudo:
        return None
    try:
        return date(int(str(crudo)[0:4]), int(str(crudo)[4:6]), int(str(crudo)[6:8]))
    except (ValueError, IndexError):
        return None


def parsear_sumario(payload: Any, *, fuente: str, fecha: date | None = None) -> list[Convocatoria]:
    """Convierte un sumario en convocatorias.

    La fecha se toma de los metadatos del sumario; ``fecha`` es el respaldo por
    si no vinieran. No se filtra por sección: se ingiere todo y se filtra al
    buscar, para no tener que reingerir si el usuario cambia sus watchlists.
    """
    sumario = _sumario_de(payload)
    fecha_publicacion = fecha_del_sumario(payload) or fecha
    if fecha_publicacion is None:
        raise ParseoSumarioError("el sumario no declara fecha y no se ha pasado ninguna")

    convocatorias: list[Convocatoria] = []
    for diario in _como_lista(sumario.get("diario")):
        for seccion in _como_lista(diario.get("seccion")):
            codigo = seccion.get("codigo")
            nombre_seccion = seccion.get("nombre")
            for departamento in _hijos(seccion, "departamento"):
                nombre_departamento = departamento.get("nombre")

                # Los items pueden estar bajo un epígrafe o directamente bajo el
                # departamento. Se recorren los dos sitios.
                bloques: list[tuple[str | None, list[Any]]] = [
                    (epigrafe.get("nombre"), _hijos(epigrafe, "item"))
                    for epigrafe in _hijos(departamento, "epigrafe")
                ]
                bloques.append((None, _hijos(departamento, "item")))

                for nombre_epigrafe, items in bloques:
                    for item in items:
                        convocatoria = _item_a_convocatoria(
                            item,
                            fuente=fuente,
                            fecha_publicacion=fecha_publicacion,
                            codigo_seccion=codigo,
                            nombre_seccion=nombre_seccion,
                            departamento=nombre_departamento,
                            epigrafe=nombre_epigrafe,
                        )
                        if convocatoria is not None:
                            convocatorias.append(convocatoria)

    return convocatorias


def _item_a_convocatoria(
    item: Any,
    *,
    fuente: str,
    fecha_publicacion: date,
    codigo_seccion: str | None,
    nombre_seccion: str | None,
    departamento: str | None,
    epigrafe: str | None,
) -> Convocatoria | None:
    """Mapea un item del sumario, o ``None`` si le falta lo imprescindible.

    Se descarta en silencio un item sin identificador o sin enlaces en vez de
    romper la ingesta del día entero: una convocatoria perdida es mejor que 270.
    """
    if not isinstance(item, dict):
        return None

    identificador = item.get("identificador")
    url_html = item.get("url_html")
    url_xml = item.get("url_xml")
    if not identificador or not url_html or not url_xml:
        return None

    url_pdf = item.get("url_pdf")
    if isinstance(url_pdf, dict):
        url_pdf = url_pdf.get("texto")
    elif not isinstance(url_pdf, str):
        url_pdf = None

    return Convocatoria(
        fuente=fuente,
        identificador=identificador,
        titulo=item.get("titulo") or identificador,
        seccion=Seccion.from_api_code(codigo_seccion),
        seccion_nombre=nombre_seccion,
        departamento=departamento,
        epigrafe=epigrafe,
        control=item.get("control"),
        fecha_publicacion=fecha_publicacion,
        url_html=url_html,
        url_xml=url_xml,
        url_pdf=url_pdf,
    )
