"""Interfaz de línea de comandos: punto de entrada y diagnóstico.

    convocatorias-mcp                      arranca el servidor MCP por stdio
    convocatorias-mcp doctor               revisa la instalación
    convocatorias-mcp doctor --check-api   además comprueba el acceso al BOE
    convocatorias-mcp ingest               puebla el índice local

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
from convocatorias_mcp.ingest import Progreso, ResumenIngesta, ingerir
from convocatorias_mcp.models import Watchlist
from convocatorias_mcp.sources import FUENTE_POR_DEFECTO, crear_fuente, nombres_de_fuentes
from convocatorias_mcp.sources.base import (
    DEFAULT_TIMEOUT_SECONDS,
    BoletinSource,
    user_agent,
)

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
