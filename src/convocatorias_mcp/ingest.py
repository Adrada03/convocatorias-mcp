"""Ingesta incremental: descarga día a día y guarda en el índice.

La API del BOE devuelve sumarios por fecha, así que la ingesta recorre fechas.
Tres propiedades que importan:

* **Incremental.** Solo se piden las fechas que no están resueltas en
  ``source_health``. Las que quedaron en error sí se reintentan.
* **Idempotente.** Reingerir una fecha actualiza las filas en vez de
  duplicarlas, porque la clave es el identificador oficial.
* **Un día sin publicación no es un fallo.** Se registra como tal y la ingesta
  continúa. No cuenta como fuente degradada ni se reintenta en cada ejecución.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from convocatorias_mcp.index import fechas_registradas, guardar_convocatorias, registrar_ingesta
from convocatorias_mcp.models import EstadoIngesta, SourceHealth
from convocatorias_mcp.sources.base import BoletinSource, SinPublicacion, SourceError

__all__ = ["Progreso", "ResumenIngesta", "ingerir", "rango_de_fechas"]


@dataclass(frozen=True)
class Progreso:
    """Un día resuelto, para que quien llame pueda informar del avance.

    Un backfill de 90 días son 90 peticiones espaciadas 1,5 s: varios minutos
    en los que, sin esto, no se vería nada.
    """

    indice: int
    total: int
    fecha: date
    estado: EstadoIngesta
    items: int = 0
    detalle: str | None = None

    def linea(self) -> str:
        """Una línea legible, pensada para la consola."""
        marca = {
            EstadoIngesta.OK: "ok",
            EstadoIngesta.SIN_PUBLICACION: "sin publicación",
            EstadoIngesta.ERROR: "ERROR",
        }[self.estado]
        contador = f"[{self.indice:>3}/{self.total}]"
        if self.estado is EstadoIngesta.OK:
            return f"{contador} {self.fecha.isoformat()}  {marca}  {self.items} convocatorias"
        if self.estado is EstadoIngesta.ERROR:
            return f"{contador} {self.fecha.isoformat()}  {marca}  {self.detalle}"
        return f"{contador} {self.fecha.isoformat()}  {marca}"


@dataclass
class ResumenIngesta:
    """Qué ha pasado en una ejecución de la ingesta."""

    fuente: str
    dias_pedidos: int = 0
    dias_omitidos: int = 0
    convocatorias: int = 0
    dias_sin_publicacion: int = 0
    errores: list[tuple[date, str]] = field(default_factory=list)

    def resumen(self) -> str:
        partes = [
            f"{self.convocatorias} convocatorias de {self.dias_pedidos} días pedidos",
        ]
        if self.dias_omitidos:
            partes.append(f"{self.dias_omitidos} días ya ingeridos")
        if self.dias_sin_publicacion:
            partes.append(f"{self.dias_sin_publicacion} sin publicación")
        if self.errores:
            partes.append(f"{len(self.errores)} con error")
        return ", ".join(partes)


def rango_de_fechas(desde: date, hasta: date) -> list[date]:
    """Todas las fechas del rango, de la más reciente a la más antigua.

    Se empieza por lo nuevo para que una ingesta interrumpida deje el índice
    con lo más útil, que es lo que acaba de publicarse.
    """
    if desde > hasta:
        return []
    dias = (hasta - desde).days
    return [hasta - timedelta(days=offset) for offset in range(dias + 1)]


async def ingerir(
    conn: sqlite3.Connection,
    source: BoletinSource,
    *,
    desde: date,
    hasta: date,
    forzar: bool = False,
    on_progress: Callable[[Progreso], None] | None = None,
) -> ResumenIngesta:
    """Ingiere el rango de fechas que falte y devuelve el resumen.

    Con ``forzar`` se repiten también las fechas ya resueltas, que es lo que
    hace falta si el parser cambia y hay que releer lo guardado.

    ``on_progress`` se llama una vez por día **pedido**, no por día del rango:
    los que ya estaban resueltos no generan ruido.
    """
    resumen = ResumenIngesta(fuente=source.name)
    ya_resueltas = set() if forzar else fechas_registradas(conn, source.name)

    pendientes = [fecha for fecha in rango_de_fechas(desde, hasta) if fecha not in ya_resueltas]
    resumen.dias_omitidos = len(rango_de_fechas(desde, hasta)) - len(pendientes)
    total = len(pendientes)

    def avisar(indice: int, fecha: date, estado: EstadoIngesta, **extra: object) -> None:
        if on_progress is not None:
            on_progress(Progreso(indice=indice, total=total, fecha=fecha, estado=estado, **extra))  # type: ignore[arg-type]

    for indice, fecha in enumerate(pendientes, start=1):
        resumen.dias_pedidos += 1
        try:
            convocatorias = await source.fetch_dia(fecha)
        except SinPublicacion:
            # Información válida, no un error: se registra y se sigue.
            registrar_ingesta(
                conn,
                SourceHealth(fuente=source.name, fecha=fecha, estado=EstadoIngesta.SIN_PUBLICACION),
            )
            resumen.dias_sin_publicacion += 1
            avisar(indice, fecha, EstadoIngesta.SIN_PUBLICACION)
            continue
        except SourceError as exc:
            registrar_ingesta(
                conn,
                SourceHealth(
                    fuente=source.name,
                    fecha=fecha,
                    estado=EstadoIngesta.ERROR,
                    detalle=exc.message,
                ),
            )
            resumen.errores.append((fecha, exc.message))
            avisar(indice, fecha, EstadoIngesta.ERROR, detalle=exc.message)
            continue

        guardadas = guardar_convocatorias(conn, convocatorias)
        registrar_ingesta(
            conn,
            SourceHealth(fuente=source.name, fecha=fecha, estado=EstadoIngesta.OK, items=guardadas),
        )
        resumen.convocatorias += guardadas
        avisar(indice, fecha, EstadoIngesta.OK, items=guardadas)

    return resumen
