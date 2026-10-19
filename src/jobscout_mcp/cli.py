"""Interfaz de línea de comandos: punto de entrada y diagnóstico.

    jobscout-mcp                      arranca el servidor MCP por stdio
    jobscout-mcp doctor               revisa la instalación
    jobscout-mcp doctor --check-api   además hace una petición real a Adzuna

``doctor`` existe porque este servidor lo lanza Claude Desktop en segundo
plano: si algo está mal configurado, el usuario solo ve que "no funciona", sin
mensaje de error en ninguna parte. Este comando es el sitio donde mirar.

Nunca imprime el valor de una credencial, solo si está configurada y cuántos
caracteres tiene: su salida está pensada para poder pegarse en un issue.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from jobscout_mcp import __version__
from jobscout_mcp.config import ConfigError, Settings, load_profile
from jobscout_mcp.sources.base import DEFAULT_TIMEOUT_SECONDS, user_agent

__all__ = ["main"]

ADZUNA_API_BASE = "https://api.adzuna.com/v1/api"

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
        f"no hay {path}; copia .env.example a .env si quieres usar Adzuna",
    )


def check_adzuna_credentials(settings: Settings) -> list[Check]:
    checks = [
        Check(
            "ADZUNA_APP_ID",
            "ok" if settings.adzuna_app_id else "warn",
            settings.describe_secret("adzuna_app_id"),
        ),
        Check(
            "ADZUNA_APP_KEY",
            "ok" if settings.adzuna_app_key else "warn",
            settings.describe_secret("adzuna_app_key"),
        ),
        Check("ADZUNA_COUNTRY", "ok", settings.adzuna_country),
    ]
    if not settings.has_adzuna_credentials:
        checks.append(
            Check(
                "Adzuna",
                "warn",
                "sin credenciales la fuente se marcará como 'skipped'; "
                "regístrate gratis en https://developer.adzuna.com/signup",
            )
        )
    return checks


def check_profile(settings: Settings) -> Check:
    try:
        profile = load_profile(settings.profile_path)
    except ConfigError as exc:
        return Check("profile.yaml", "fail", str(exc))

    if profile is None:
        return Check(
            "profile.yaml",
            "warn",
            f"no hay {settings.profile_path}; se ordenará por fecha en vez de por afinidad",
        )
    if profile.is_empty:
        return Check("profile.yaml", "warn", f"{settings.profile_path} no define nada útil")

    # Se informan recuentos, no contenidos: la salida es pegable en un issue.
    resumen = (
        f"{len(profile.skills)} skills, {len(profile.nice_to_have)} nice-to-have, "
        f"{len(profile.locations)} ubicaciones, seniority={profile.seniority or 'sin definir'}"
    )
    return Check("profile.yaml", "ok", f"{settings.profile_path} ({resumen})")


def check_cache(settings: Settings) -> Check:
    path = settings.cache_path
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check("caché", "fail", f"no se puede crear {parent}: {exc}")

    probe = parent / ".jobscout-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("caché", "fail", f"{parent} no es escribible: {exc}")

    estado = "existe" if path.is_file() else "se creará al primer uso"
    return Check("caché", "ok", f"{path} ({estado}), TTL {settings.cache_ttl_seconds}s")


def check_adzuna_api(
    settings: Settings, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> list[Check]:
    """Hace una petición real a Adzuna y comprueba que el país responde.

    Es el único sitio del proyecto que gasta cuota de la API a propósito: una
    petición con ``results_per_page=1``, la más barata posible.
    """
    if not settings.has_adzuna_credentials:
        return [Check("Adzuna /search", "warn", "omitido: faltan credenciales")]

    country = settings.adzuna_country
    url = f"{ADZUNA_API_BASE}/jobs/{country}/search/1"
    params = {
        "app_id": settings.adzuna_app_id or "",
        "app_key": settings.adzuna_app_key or "",
        "what": "python",
        "results_per_page": 1,
        "content-type": "application/json",
    }

    try:
        response = httpx.get(
            url,
            params=params,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent(), "Accept": "application/json"},
        )
    except httpx.RequestError as exc:
        return [Check("Adzuna /search", "fail", f"fallo de red: {exc}")]

    # La URL nunca se imprime: lleva las credenciales en la query.
    etiqueta = f"Adzuna /jobs/{country}/search"

    if response.status_code in (401, 403):
        return [Check(etiqueta, "fail", f"HTTP {response.status_code}: credenciales rechazadas")]
    if response.status_code != 200:
        # Sin credenciales válidas la API responde HTML, no JSON: merece un
        # mensaje propio porque el cuerpo no se puede parsear.
        tipo = response.headers.get("content-type", "desconocido")
        return [
            Check(
                etiqueta,
                "fail",
                f"HTTP {response.status_code} ({tipo}); "
                f"¿existe el país '{country}' y son correctas las credenciales?",
            )
        ]

    try:
        data = response.json()
    except ValueError:
        return [Check(etiqueta, "fail", "HTTP 200 pero la respuesta no es JSON")]

    resultados = data.get("results") or []
    checks = [
        Check(
            etiqueta,
            "ok",
            f"HTTP 200, {data.get('count', '?')} ofertas para 'python' en '{country}'",
        )
    ]
    if resultados:
        checks.append(
            Check(
                "Campos devueltos",
                "ok",
                ", ".join(sorted(resultados[0].keys())),
            )
        )
    else:
        checks.append(
            Check(
                "Campos devueltos",
                "warn",
                f"la respuesta no trae resultados; '{country}' puede no tener cobertura",
            )
        )
    return checks


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #


def run_doctor(*, check_api: bool = False, cwd: Path | None = None) -> int:
    """Imprime el diagnóstico. Devuelve 0 si todo está bien, 1 si algo falla."""
    cwd = cwd or Path.cwd()
    print(f"jobscout-mcp {__version__}")
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

    checks.extend(check_adzuna_credentials(settings))
    checks.append(check_profile(settings))
    checks.append(check_cache(settings))

    if check_api:
        checks.extend(check_adzuna_api(settings))
    else:
        checks.append(
            Check(
                "Adzuna /search",
                "warn",
                "no comprobado; usa --check-api para probarlo de verdad",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobscout-mcp",
        description="Servidor MCP de búsqueda agregada de ofertas de empleo.",
    )
    parser.add_argument("--version", action="version", version=f"jobscout-mcp {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser(
        "doctor",
        help="Revisa la instalación: credenciales, perfil, caché y acceso a la API.",
        description=(
            "Revisa la instalación. Sin --check-api no sale a la red, así que no "
            "gasta cuota. Nunca imprime el valor de una credencial."
        ),
    )
    doctor.add_argument(
        "--check-api",
        action="store_true",
        help="Hace una petición real a Adzuna (gasta 1 de las 250 diarias).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada del comando ``jobscout-mcp``."""
    # La salida lleva acentos y la consola de Windows no siempre usa UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    args = build_parser().parse_args(argv)

    if args.command == "doctor":
        return run_doctor(check_api=args.check_api)

    # Sin subcomando se arranca el servidor MCP, que todavía no existe.
    print(
        "El servidor MCP aún no está implementado.\n"
        "Mientras tanto, 'jobscout-mcp doctor' revisa la instalación.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
