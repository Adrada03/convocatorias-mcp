"""Interfaz de línea de comandos: punto de entrada y diagnóstico.

    convocatorias-mcp                      arranca el servidor MCP por stdio
    convocatorias-mcp doctor               revisa la instalación
    convocatorias-mcp doctor --check-api   además comprueba el acceso al BOE
    convocatorias-mcp ingest               puebla el índice local
    convocatorias-mcp export               genera el JSON del dashboard

``doctor`` existe porque este servidor lo lanza Claude Desktop en segundo
plano: si algo está mal configurado, el usuario solo ve que "no funciona", sin
mensaje de error en ninguna parte. Este comando es el sitio donde mirar.

Nunca imprime credenciales ni datos personales, ni completos ni parciales: la
salida está pensada para poder pegarse en un issue.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import httpx

from convocatorias_mcp import __version__
from convocatorias_mcp.config import ConfigError, Settings, hoy_boletin, load_watchlists
from convocatorias_mcp.db import IndexSchemaError, estado_del_indice, open_index
from convocatorias_mcp.export import CITA_BOE, VENTANA_POR_DEFECTO, exportar
from convocatorias_mcp.index import contar_por_watchlist
from convocatorias_mcp.ingest import Progreso, ResumenIngesta, ingerir
from convocatorias_mcp.models import Watchlist
from convocatorias_mcp.sources import FUENTE_POR_DEFECTO, crear_fuente, nombres_de_fuentes
from convocatorias_mcp.sources.base import (
    DEFAULT_TIMEOUT_SECONDS,
    BoletinSource,
    user_agent,
)
from convocatorias_mcp.vocabulario import cargar_sinonimos

__all__ = ["main"]

BOE_SUMARIO_URL = "https://www.boe.es/datosabiertos/api/boe/sumario"

Status = Literal["ok", "warn", "fail"]

_MARKERS: dict[Status, str] = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[FALL]"}


@dataclass(frozen=True)
class Check:
    """Resultado de una comprobación, listo para imprimir."""

    name: str
    status: Status
    detail: str

    def render(self) -> str:
        return f"{_MARKERS[self.status]} {self.name}: {self.detail}"


# --------------------------------------------------------------------------- #
# Comprobaciones
# --------------------------------------------------------------------------- #


def check_python() -> Check:
    # No se comprueba el mínimo: con requires-python >= 3.11, un intérprete
    # anterior ni llega aquí porque falla al importar los modelos.
    return Check("Python", "ok", ".".join(str(part) for part in sys.version_info[:3]))


def check_dotenv(cwd: Path) -> Check:
    path = cwd / ".env"
    if path.is_file():
        return Check(".env", "ok", f"encontrado en {path}")
    return Check(
        ".env",
        "warn",
        f"no hay {path}; solo hace falta para cambiar rutas o ajustes",
    )


def check_watchlists(settings: Settings) -> Check:
    try:
        config = load_watchlists(settings.watchlists_path)
    except ConfigError as exc:
        return Check("watchlists.yaml", "fail", str(exc))

    if config is None:
        return Check(
            "watchlists.yaml",
            "warn",
            f"no hay {settings.watchlists_path}; se ordenará por fecha y no se puntuará",
        )
    if config.is_empty:
        return Check(
            "watchlists.yaml", "warn", f"{settings.watchlists_path} no define ninguna watchlist"
        )

    # Los nombres de las watchlists los eligió el usuario y no son secretos,
    # pero sus keywords pueden decir bastante de él: se cuentan, no se listan.
    resumen = ", ".join(
        f"{watchlist.nombre} ({_plural(len(watchlist.keywords), 'keyword', 'keywords')}, "
        f"{_secciones(watchlist)})"
        for watchlist in config.watchlists
    )
    return Check("watchlists.yaml", "ok", f"{len(config.watchlists)}: {resumen}")


#: Por debajo de esto, una watchlist casi seguro está mal configurada y no es
#: que no haya convocatorias. Es un umbral de sospecha, no una regla: una
#: watchlist deliberadamente estrecha puede casar 3 y estar bien. Por eso el
#: aviso sugiere revisar, y nunca afirma que esté rota.
UMBRAL_WATCHLIST_SOSPECHOSA = 5


def check_watchlists_casan(settings: Settings) -> list[Check]:
    """Comprueba que cada watchlist casa con algo del índice.

    Una watchlist mal configurada no da ningún error: simplemente no devuelve
    nada, y desde fuera se confunde con "no ha salido nada que te encaje". Esa
    confusión es justo lo que hay que evitar, así que se cuenta de verdad.

    Cuando una casa poco, se vuelve a contar con ``min_score`` a cero. Si así
    aparecen muchas más, el problema es el umbral y no las keywords, y se dice
    cuál de los dos es: es la diferencia entre "no hay nada" y "lo estás
    cortando tú".
    """
    try:
        config = load_watchlists(settings.watchlists_path)
    except ConfigError:
        return []  # check_watchlists ya lo ha reportado.
    if config is None or config.is_empty:
        return []

    try:
        conn = open_index(settings.index_path)
    except (IndexSchemaError, OSError):
        return []  # check_indice ya lo ha reportado.

    try:
        if estado_del_indice(conn).convocatorias == 0:
            # Sin nada indexado no se puede distinguir una watchlist rota de
            # una que aún no tiene nada que casar. Decirlo sería adivinar.
            # Se mira el recuento y no `esta_vacio`, que cuenta días ingeridos:
            # un índice con convocatorias y sin registro de ingesta sí se puede
            # diagnosticar, y saltárselo era volver al fallo silencioso.
            return []
        sinonimos = cargar_sinonimos(settings.sinonimos_path)
        checks: list[Check] = []
        for watchlist in config.watchlists:
            casan = contar_por_watchlist(conn, watchlist, sinonimos=sinonimos)
            if casan > UMBRAL_WATCHLIST_SOSPECHOSA:
                checks.append(
                    Check(f"watchlist '{watchlist.nombre}'", "ok", f"casa {casan} convocatorias")
                )
                continue

            detalle = f"casa {casan} convocatorias del índice"
            if watchlist.min_score > 0:
                sin_umbral = contar_por_watchlist(
                    conn, watchlist, sinonimos=sinonimos, min_score=0.0
                )
                if sin_umbral > casan:
                    detalle += (
                        f"; con min_score 0 casaría {sin_umbral}, así que es el umbral "
                        f"({watchlist.min_score}) el que las está cortando, no las keywords"
                    )
                else:
                    detalle += "; revisa las keywords: no están en el vocabulario del boletín"
            else:
                detalle += "; revisa las keywords: no están en el vocabulario del boletín"
            checks.append(Check(f"watchlist '{watchlist.nombre}'", "warn", detalle))
        return checks
    finally:
        conn.close()


def _plural(cantidad: int, singular: str, plural: str) -> str:
    return f"{cantidad} {singular if cantidad == 1 else plural}"


def _secciones(watchlist: Watchlist) -> str:
    if watchlist.vigila_todas_las_secciones:
        return "todas las secciones"
    return _plural(len(watchlist.secciones), "sección", "secciones")


def check_index(settings: Settings) -> list[Check]:
    try:
        conn = open_index(settings.index_path)
    except IndexSchemaError as exc:
        return [Check("índice", "fail", str(exc))]
    except OSError as exc:
        return [Check("índice", "fail", f"no se puede abrir {settings.index_path}: {exc}")]

    try:
        estado = estado_del_indice(conn)
    finally:
        conn.close()

    checks = [Check("índice", "ok", f"{settings.index_path}")]
    if estado.esta_vacio:
        checks.append(Check("contenido del índice", "warn", estado.resumen()))
    else:
        nivel: Status = "warn" if (estado.huecos or estado.dias_con_error) else "ok"
        checks.append(Check("contenido del índice", nivel, estado.resumen()))
        if estado.huecos:
            muestra = ", ".join(dia.isoformat() for dia in estado.huecos[:5])
            resto = f" y {len(estado.huecos) - 5} más" if len(estado.huecos) > 5 else ""
            checks.append(
                Check(
                    "huecos",
                    "warn",
                    f"{muestra}{resto}; 'convocatorias-mcp ingest' los rellena",
                )
            )
    return checks


def check_boe(*, timeout: float = DEFAULT_TIMEOUT_SECONDS, hoy: date | None = None) -> list[Check]:
    """Comprueba que la API del BOE responde.

    Un 404 cuenta como éxito: significa que la API contesta y que ese día no
    hubo publicación, que es exactamente lo que pasa los domingos. Solo un
    error de red o un 5xx indican un problema.
    """
    fecha = hoy or hoy_boletin()
    url = f"{BOE_SUMARIO_URL}/{fecha.strftime('%Y%m%d')}"

    try:
        response = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent(), "Accept": "application/json"},
        )
    except httpx.RequestError as exc:
        return [Check("API del BOE", "fail", f"fallo de red: {exc}")]

    if response.status_code == 200:
        return [
            Check(
                "API del BOE",
                "ok",
                f"sumario del {fecha.isoformat()}: HTTP 200, {len(response.content):,} bytes",
            )
        ]
    if response.status_code == 404:
        return [
            Check(
                "API del BOE",
                "ok",
                f"responde correctamente; el {fecha.isoformat()} no hubo publicación",
            )
        ]
    return [
        Check("API del BOE", "fail", f"HTTP {response.status_code} al pedir el sumario del {fecha}")
    ]


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #


def run_doctor(*, check_api: bool = False, cwd: Path | None = None) -> int:
    """Imprime el diagnóstico. Devuelve 0 si todo está bien, 1 si algo falla."""
    cwd = cwd or Path.cwd()
    print(f"convocatorias-mcp {__version__}")
    print(f"User-Agent: {user_agent()}\n")

    checks: list[Check] = [check_python(), check_dotenv(cwd)]

    try:
        settings = Settings.from_env(cwd=cwd)
    except ConfigError as exc:
        checks.append(Check("configuración", "fail", str(exc)))
        for check in checks:
            print(check.render())
        print("\nHay 1 problema que impide continuar.")
        return 1

    checks.append(check_watchlists(settings))
    checks.extend(check_index(settings))
    checks.extend(check_watchlists_casan(settings))

    if check_api:
        checks.extend(check_boe())
    else:
        checks.append(
            Check(
                "API del BOE",
                "warn",
                "no comprobada; usa --check-api para probar la conectividad",
            )
        )

    for check in checks:
        print(check.render())

    fallos = sum(1 for check in checks if check.status == "fail")
    avisos = sum(1 for check in checks if check.status == "warn")
    print()
    if fallos:
        print(f"{fallos} problema(s) que impiden funcionar, {avisos} aviso(s).")
        return 1
    if avisos:
        print(f"Sin problemas graves. {avisos} aviso(s): el servidor funcionará con límites.")
        return 0
    print("Todo en orden.")
    return 0


async def _ingerir_y_cerrar(
    conn: sqlite3.Connection,
    source: BoletinSource,
    *,
    desde: date,
    hasta: date,
    forzar: bool,
    silencioso: bool,
) -> ResumenIngesta:
    """Envuelve la ingesta para cerrar siempre el cliente HTTP de la fuente."""

    def informar(progreso: Progreso) -> None:
        # flush porque un backfill tarda minutos y la salida se vería a trozos.
        print(progreso.linea(), flush=True)

    try:
        return await ingerir(
            conn,
            source,
            desde=desde,
            hasta=hasta,
            forzar=forzar,
            on_progress=None if silencioso else informar,
        )
    finally:
        await source.aclose()


def run_ingest(
    *,
    fuente: str = FUENTE_POR_DEFECTO,
    backfill: int | None = None,
    forzar: bool = False,
    silencioso: bool = False,
) -> int:
    """Puebla el índice local. Devuelve 0 si fue bien, 1 si hubo errores.

    Por defecto usa el BOE. ``fixture`` lee sumarios guardados en disco y sirve
    para desarrollar o probar el proyecto sin red.
    """
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuración inválida: {exc}", file=sys.stderr)
        return 1

    try:
        source = crear_fuente(fuente)
    except KeyError as exc:
        print(str(exc).strip("\"'"), file=sys.stderr)
        return 1

    if not source.is_available():
        print(f"La fuente {source.name} no está disponible.", file=sys.stderr)
        return 1

    dias = backfill if backfill is not None else settings.backfill_dias
    hasta = hoy_boletin()
    desde = hasta - timedelta(days=dias - 1)

    print(f"Ingiriendo {source.display_name}: {desde.isoformat()} a {hasta.isoformat()}")
    if source.licencia_url:
        print(f"Datos reutilizados bajo {source.licencia_url}")
    print()

    conn = open_index(settings.index_path)
    try:
        resumen = asyncio.run(
            _ingerir_y_cerrar(
                conn,
                source,
                desde=desde,
                hasta=hasta,
                forzar=forzar,
                silencioso=silencioso,
            )
        )
        estado = estado_del_indice(conn, fuente=source.name)
    finally:
        conn.close()

    print()
    print(resumen.resumen())
    print(f"Índice: {estado.resumen()}")
    if source.cita_requerida:
        print(source.cita_requerida)
    return 1 if resumen.errores else 0


def run_export(*, dias: int | None = VENTANA_POR_DEFECTO, salida: Path | None = None) -> int:
    """Genera el JSON que consume el dashboard."""
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuración inválida: {exc}", file=sys.stderr)
        return 1

    try:
        config = load_watchlists(settings.watchlists_path)
    except ConfigError as exc:
        # Sin watchlists se exporta igual: lo que no se puede es exportar con
        # unas watchlists que el usuario cree que valen y no valen.
        print(f"watchlists.yaml tiene errores: {exc}", file=sys.stderr)
        return 1

    destino = salida or Path("dashboard") / "data.json"
    conn = open_index(settings.index_path)
    try:
        resumen = exportar(
            conn,
            destino,
            config=config,
            sinonimos=cargar_sinonimos(settings.sinonimos_path),
            dias=dias,
        )
    finally:
        conn.close()

    print(f"Escrito {resumen.ruta}")
    print(resumen.resumen())
    if resumen.cubre_hasta:
        print(f"Los datos llegan hasta el {resumen.cubre_hasta.isoformat()}")
    if resumen.exportadas == 0:
        print(
            "El índice está vacío: ejecuta 'convocatorias-mcp ingest' antes de exportar.",
            file=sys.stderr,
        )
        return 1
    print(CITA_BOE)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="convocatorias-mcp",
        description=(
            "Servidor MCP que vigila los boletines oficiales españoles y encuentra "
            "convocatorias que encajan con tu perfil."
        ),
    )
    parser.add_argument("--version", action="version", version=f"convocatorias-mcp {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser(
        "doctor",
        help="Revisa la instalación: watchlists, índice y acceso al BOE.",
        description=(
            "Revisa la instalación. Sin --check-api no sale a la red. Nunca imprime "
            "credenciales ni datos personales."
        ),
    )
    doctor.add_argument(
        "--check-api",
        action="store_true",
        help="Comprueba de verdad que la API del BOE responde.",
    )
    ingest = subparsers.add_parser(
        "ingest",
        help="Puebla el índice local con los sumarios que falten.",
        description=(
            "Descarga los sumarios de las fechas que falten y los guarda en el índice. "
            "Es incremental: las fechas ya resueltas no se vuelven a pedir."
        ),
    )
    ingest.add_argument(
        "--backfill",
        type=int,
        metavar="N",
        help="Días hacia atrás a ingerir (por defecto, los de la configuración).",
    )
    ingest.add_argument(
        "--forzar",
        action="store_true",
        help="Repite también las fechas ya ingeridas.",
    )
    ingest.add_argument(
        "--fuente",
        default=FUENTE_POR_DEFECTO,
        choices=nombres_de_fuentes(),
        help=(
            "Boletín a ingerir. 'fixture' lee sumarios guardados en disco y no "
            f"toca la red (por defecto: {FUENTE_POR_DEFECTO})."
        ),
    )
    ingest.add_argument(
        "--silencioso",
        action="store_true",
        help="No imprime una línea por día; solo el resumen final.",
    )

    export = subparsers.add_parser(
        "export",
        help="Genera el JSON que consume el dashboard.",
        description=(
            "Exporta los metadatos del índice a JSON. Nunca los textos completos, "
            "que viven en el BOE y se enlazan. Por defecto recorta a los últimos "
            f"{VENTANA_POR_DEFECTO} días más todo lo que coincida con alguna watchlist, "
            "porque el índice entero son 15.000 registros que un móvil tiene que "
            "parsear."
        ),
    )
    export.add_argument(
        "--dias",
        type=int,
        metavar="N",
        default=VENTANA_POR_DEFECTO,
        help=f"Ventana en días hacia atrás (por defecto {VENTANA_POR_DEFECTO}).",
    )
    export.add_argument(
        "--todo",
        action="store_true",
        help="Exporta el índice completo, sin recortar.",
    )
    export.add_argument(
        "--salida",
        type=Path,
        metavar="RUTA",
        help="Dónde escribir el JSON (por defecto dashboard/data.json).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada del comando ``convocatorias-mcp``."""
    # La salida lleva acentos y la consola de Windows no siempre usa UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    args = build_parser().parse_args(argv)

    if args.command == "doctor":
        return run_doctor(check_api=args.check_api)

    if args.command == "export":
        return run_export(dias=None if args.todo else args.dias, salida=args.salida)

    if args.command == "ingest":
        return run_ingest(
            fuente=args.fuente,
            backfill=args.backfill,
            forzar=args.forzar,
            silencioso=args.silencioso,
        )

    # Sin subcomando se arranca el servidor MCP por stdio, que es como lo
    # invoca Claude Desktop. A partir de aqui stdout pertenece al protocolo.
    from convocatorias_mcp.server import main as server_main

    return server_main()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
