"""Exporta los metadatos del índice al JSON que consume el dashboard.

Solo metadatos: **nunca textos completos**. El texto oficial vive en el BOE y
el dashboard enlaza a él. Y ni siquiera se exporta la URL, porque se deriva del
identificador (``https://www.boe.es/diario_boe/txt.php?id=`` + identificador):
eran 940 KB de repetir la misma cadena 15.000 veces.

Tamaños medidos sobre el índice real de 14.935 convocatorias:

===================================== ========== =========
Variante                               Crudo      Gzip
===================================== ========== =========
Todo, claves legibles                  7,31 MB    926 KB
Todo, claves cortas sin campos vacíos  4,42 MB    747 KB
30 días + coincidencias (por defecto)  ~1,8 MB    ~290 KB
===================================== ========== =========

Por eso el recorte por defecto: 4,4 MB de JSON son 15.000 objetos que un móvil
tiene que parsear y mantener en memoria. Con ``--todo`` se exporta el índice
completo, que gzipped son 747 KB y en escritorio va sobrado.

Las claves de cada registro son cortas a propósito, y los campos vacíos se
omiten. No es microoptimización: es la diferencia entre 7,3 MB y 4,4 MB.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from convocatorias_mcp.db import estado_del_indice
from convocatorias_mcp.index import (
    _CAMPOS,
    _a_convocatoria,
)
from convocatorias_mcp.models import Convocatoria, Estado, Seccion, WatchlistsConfig
from convocatorias_mcp.ranking import PESOS_POR_DEFECTO, Pesos, puntuar
from convocatorias_mcp.sources import FUENTES

__all__ = [
    "VENTANA_POR_DEFECTO",
    "ResumenExport",
    "atribucion",
    "construir_datos",
    "exportar",
]

#: Días hacia atrás que se exportan por defecto.
VENTANA_POR_DEFECTO = 30

#: Prefijo con el que el dashboard reconstruye la URL oficial.
URL_BASE = "https://www.boe.es/diario_boe/txt.php?id="

#: Cita y licencia de respaldo, si el índice tuviera datos de una fuente que ya
#: no está registrada. Lo normal es que las declare la propia fuente.
CITA_BOE = "Fuente de los datos: Agencia Estatal Boletín Oficial del Estado"
LICENCIA_BOE = "https://www.boe.es/informacion/aviso_legal/index.php#reutilizacion"


def atribucion(conn: sqlite3.Connection) -> list[dict[str, str | None]]:
    """Cita y licencia de cada fuente presente en el índice.

    Se leen de la propia fuente (``cita_requerida`` y ``licencia_url``), que es
    donde se declaran, en vez de repetirlas aquí: la licencia del BOE obliga a
    citar, y ese requisito se arrastra desde un único sitio.
    """
    presentes = [
        fila["fuente"] for fila in conn.execute("SELECT DISTINCT fuente FROM convocatorias")
    ]
    atribuciones: list[dict[str, str | None]] = []
    for nombre in sorted(presentes):
        clase = FUENTES.get(nombre)
        atribuciones.append(
            {
                "fuente": nombre,
                "nombre": clase.display_name if clase else nombre,
                "cita": (clase.cita_requerida if clase else None) or CITA_BOE,
                "licencia": (clase.licencia_url if clase else None) or LICENCIA_BOE,
            }
        )
    return atribuciones


@dataclass(frozen=True)
class ResumenExport:
    """Qué se ha exportado, para contarlo por consola."""

    ruta: Path
    exportadas: int
    total_indice: int
    recortado: bool
    dias: int | None
    coincidentes: int
    cubre_hasta: date | None
    tamano_bytes: int

    def resumen(self) -> str:
        tamano = (
            f"{self.tamano_bytes / 1_048_576:.2f} MB"
            if self.tamano_bytes > 1_048_576
            else f"{self.tamano_bytes / 1024:.0f} KB"
        )
        if not self.recortado:
            return f"{self.exportadas:,} convocatorias (todo el índice), {tamano}"
        return (
            f"{self.exportadas:,} de {self.total_indice:,} convocatorias, {tamano} "
            f"(últimos {self.dias} días más {self.coincidentes} coincidencias)"
        )


def _registro(convocatoria: Convocatoria, matches: list[str], score: float | None) -> dict:
    """Un registro con claves cortas y sin campos vacíos."""
    datos: dict[str, object] = {
        "i": convocatoria.identificador,
        "t": convocatoria.titulo,
        "f": convocatoria.fecha_publicacion.isoformat(),
        "s": convocatoria.seccion.value,
    }
    if convocatoria.departamento:
        datos["d"] = convocatoria.departamento
    if convocatoria.plazo_fin:
        datos["p"] = convocatoria.plazo_fin.isoformat()
    # "nueva" es el estado por defecto: no hace falta repetirlo 15.000 veces.
    if convocatoria.estado is not Estado.NUEVA:
        datos["e"] = convocatoria.estado.value
    if matches:
        datos["w"] = matches
        datos["sc"] = score
    return datos


def construir_datos(
    conn: sqlite3.Connection,
    *,
    config: WatchlistsConfig | None = None,
    sinonimos: Mapping[str, tuple[str, ...]] | None = None,
    pesos: Pesos = PESOS_POR_DEFECTO,
    dias: int | None = VENTANA_POR_DEFECTO,
    hoy: date | None = None,
) -> tuple[dict, ResumenExport]:
    """Construye el JSON del dashboard. ``dias=None`` exporta el índice entero.

    El recorte es "ventana reciente **más** todo lo que coincida con alguna
    watchlist": una beca de hace dos meses que encaja con el perfil es
    exactamente lo que no se puede perder por recortar.
    """
    referencia = hoy or date.today()
    corte = referencia - timedelta(days=dias) if dias is not None else None

    filas = conn.execute(
        f"""
        SELECT {_CAMPOS}
        FROM convocatorias c
        LEFT JOIN seguimiento s ON s.convocatoria_id = c.id
        ORDER BY c.fecha_publicacion DESC, c.seccion ASC, c.identificador ASC
        """
    ).fetchall()

    registros: list[dict] = []
    coincidentes = 0
    watchlists = list(config.watchlists) if config and not config.is_empty else []

    for fila in filas:
        convocatoria = _a_convocatoria(fila)
        matches = [
            match
            for match in (
                puntuar(convocatoria, watchlist, pesos=pesos, sinonimos=sinonimos)
                for watchlist in watchlists
            )
            if match is not None
        ]
        if matches:
            coincidentes += 1

        reciente = corte is None or convocatoria.fecha_publicacion >= corte
        if not reciente and not matches:
            continue

        matches.sort(key=lambda match: (-match.total, match.watchlist))
        registros.append(
            _registro(
                convocatoria,
                [match.watchlist for match in matches],
                round(matches[0].total, 3) if matches else None,
            )
        )

    estado = estado_del_indice(conn)
    cubre_hasta = estado.ultima_fecha or estado.ultima_publicacion
    atribuciones = atribucion(conn)

    datos = {
        "generado": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cubre_hasta": cubre_hasta.isoformat() if cubre_hasta else None,
        "url_base": URL_BASE,
        "fuentes": atribuciones,
        # Atajo para la cabecera de la página; el detalle va en "fuentes".
        "cita": atribuciones[0]["cita"] if atribuciones else CITA_BOE,
        "licencia": atribuciones[0]["licencia"] if atribuciones else LICENCIA_BOE,
        "recorte": {
            "aplicado": dias is not None,
            "dias": dias,
            "exportadas": len(registros),
            "total_indice": estado.convocatorias,
            "coincidentes": coincidentes,
        },
        "watchlists": [watchlist.nombre for watchlist in watchlists],
        "secciones": {
            seccion.value: seccion.descripcion
            for seccion in Seccion
            if seccion is not Seccion.DESCONOCIDA
        },
        "estados": [estado.value for estado in Estado],
        "convocatorias": registros,
    }

    resumen = ResumenExport(
        ruta=Path(),
        exportadas=len(registros),
        total_indice=estado.convocatorias,
        recortado=dias is not None,
        dias=dias,
        coincidentes=coincidentes,
        cubre_hasta=cubre_hasta,
        tamano_bytes=0,
    )
    return datos, resumen


def exportar(
    conn: sqlite3.Connection,
    destino: Path,
    *,
    config: WatchlistsConfig | None = None,
    sinonimos: Mapping[str, tuple[str, ...]] | None = None,
    dias: int | None = VENTANA_POR_DEFECTO,
    hoy: date | None = None,
) -> ResumenExport:
    """Escribe el JSON del dashboard y devuelve el resumen."""
    datos, resumen = construir_datos(conn, config=config, sinonimos=sinonimos, dias=dias, hoy=hoy)

    # separators sin espacios: son 15.000 registros y cada espacio cuenta.
    crudo = json.dumps(datos, ensure_ascii=False, separators=(",", ":"))
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(crudo, encoding="utf-8")

    return ResumenExport(
        ruta=destino,
        exportadas=resumen.exportadas,
        total_indice=resumen.total_indice,
        recortado=resumen.recortado,
        dias=resumen.dias,
        coincidentes=resumen.coincidentes,
        cubre_hasta=resumen.cubre_hasta,
        tamano_bytes=len(crudo.encode("utf-8")),
    )
