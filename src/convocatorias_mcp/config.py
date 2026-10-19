"""Configuración del servidor: variables de entorno y carga de watchlists.

Todo lo que cambia entre instalaciones entra por aquí, y nada de esto se
versiona. Las credenciales de SMTP se leen de variables de entorno o de un
``.env`` local (ignorado por git); las watchlists, de un ``watchlists.yaml``
propio de cada usuario.

Regla que se cumple en todo el módulo: **el valor de un secreto o de un dato
personal nunca se devuelve en un mensaje, un log ni un ``repr``.**
:class:`Settings` los guarda para usarlos, pero lo que se puede imprimir es si
están o no configurados, y como mucho su longitud.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from convocatorias_mcp.models import WatchlistsConfig

__all__ = [
    "DEFAULT_BACKFILL_DIAS",
    "TZ_BOLETIN",
    "ConfigError",
    "Settings",
    "default_data_dir",
    "hoy_boletin",
    "load_watchlists",
]

#: Zona horaria de referencia. El BOE publica en hora española, así que "hoy"
#: tiene que decidirse en Europe/Madrid y no en la del servidor: si no, una
#: ingesta lanzada de madrugada desde otra zona pediría el día equivocado.
TZ_BOLETIN = ZoneInfo("Europe/Madrid")

#: Días que se descargan hacia atrás en el primer arranque.
DEFAULT_BACKFILL_DIAS = 90

DEFAULT_INDEX_FILENAME = "convocatorias-index.sqlite3"
DEFAULT_WATCHLISTS_FILENAME = "watchlists.yaml"
DEFAULT_SINONIMOS_FILENAME = "sinonimos.yaml"


class ConfigError(Exception):
    """Configuración inválida, con un mensaje que el usuario pueda accionar."""


def hoy_boletin() -> date:
    """El día de hoy según la zona horaria del boletín."""
    return datetime.now(TZ_BOLETIN).date()


def default_data_dir() -> Path:
    """Carpeta de datos del usuario, según la convención de cada sistema.

    El servidor lo arranca Claude Desktop, así que el directorio de trabajo es
    impredecible: el índice no puede vivir en una ruta relativa.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / "convocatorias-mcp"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "convocatorias-mcp"
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "convocatorias-mcp"


class Settings(BaseModel):
    """Ajustes efectivos del servidor, ya resueltos.

    Se construye con :meth:`from_env`, que es el único sitio que lee el
    entorno. El resto del código recibe un ``Settings`` ya resuelto, lo que
    hace los tests triviales: se instancia a mano y no hace falta tocar
    ``os.environ``.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Rutas ------------------------------------------------------------ #
    watchlists_path: Path
    index_path: Path
    #: Sinonimos del usuario. Si no existe el fichero se usan los de serie.
    sinonimos_path: Path | None = None

    # --- Ingesta ---------------------------------------------------------- #
    backfill_dias: int = Field(default=DEFAULT_BACKFILL_DIAS, ge=1, le=3650)

    # --- Digest por email ------------------------------------------------- #
    # repr=False en todo lo sensible: la contraseña es un secreto y el
    # destinatario es un dato personal.
    smtp_host: str | None = Field(default=None, repr=False)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str | None = Field(default=None, repr=False)
    smtp_password: str | None = Field(default=None, repr=False)
    digest_to: str | None = Field(default=None, repr=False)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        dotenv_path: Path | None = None,
        cwd: Path | None = None,
    ) -> Self:
        """Resuelve los ajustes desde el entorno y un ``.env`` opcional.

        Precedencia: variables de entorno reales por encima del ``.env``, que a
        su vez va por encima de los valores por defecto. Así se puede
        sobrescribir un valor puntual sin editar el fichero.
        """
        cwd = cwd or Path.cwd()
        env = dict(os.environ if env is None else env)

        dotenv_file = dotenv_path if dotenv_path is not None else cwd / ".env"
        if dotenv_file.is_file():
            for key, value in dotenv_values(dotenv_file).items():
                if value is not None and key not in env:
                    env[key] = value

        data_dir = default_data_dir()

        watchlists_env = env.get("CONVOCATORIAS_WATCHLISTS_PATH")
        if watchlists_env:
            watchlists_path = Path(watchlists_env).expanduser()
        else:
            # Un watchlists.yaml junto al proyecto gana al de la carpeta de
            # datos: es lo que espera quien clona el repo y copia la plantilla.
            local = cwd / DEFAULT_WATCHLISTS_FILENAME
            watchlists_path = local if local.is_file() else data_dir / DEFAULT_WATCHLISTS_FILENAME

        index_env = env.get("CONVOCATORIAS_INDEX_PATH")
        index_path = (
            Path(index_env).expanduser() if index_env else data_dir / DEFAULT_INDEX_FILENAME
        )

        sinonimos_env = env.get("CONVOCATORIAS_SINONIMOS_PATH")
        if sinonimos_env:
            sinonimos_path: Path | None = Path(sinonimos_env).expanduser()
        else:
            # Sin fichero propio se usan los sinónimos de serie, así que aquí
            # queda None en vez de una ruta que no existe.
            local_sinonimos = cwd / DEFAULT_SINONIMOS_FILENAME
            sinonimos_path = local_sinonimos if local_sinonimos.is_file() else None

        return cls(
            watchlists_path=watchlists_path,
            index_path=index_path,
            sinonimos_path=sinonimos_path,
            backfill_dias=_int_env(env, "CONVOCATORIAS_BACKFILL_DIAS", DEFAULT_BACKFILL_DIAS),
            smtp_host=_clean(env.get("SMTP_HOST")),
            smtp_port=_int_env(env, "SMTP_PORT", 587),
            smtp_user=_clean(env.get("SMTP_USER")),
            smtp_password=_clean(env.get("SMTP_PASSWORD")),
            digest_to=_clean(env.get("DIGEST_TO")),
        )

    @property
    def has_smtp(self) -> bool:
        """``True`` si el digest se puede enviar por email.

        Sin esto el digest no falla: se escribe a fichero y se avisa.
        """
        return bool(self.smtp_host and self.smtp_user and self.smtp_password and self.digest_to)

    def describe_secret(self, name: str) -> str:
        """Describe un secreto o un dato personal sin revelarlo.

        Devuelve solo su longitud, que basta para distinguir "no lo he puesto"
        de "lo he pegado mal" sin que el valor acabe en una captura de pantalla
        ni en el historial de la terminal. Se usa también con ``digest_to``:
        una dirección de email no se imprime, ni enmascarada.
        """
        value = getattr(self, name, None)
        if not value:
            return "no configurado"
        return f"configurado ({len(str(value))} caracteres)"


def _clean(value: str | None) -> str | None:
    """Limpia espacios y comillas que se cuelan al pegar un valor en un .env."""
    if value is None:
        return None
    limpio = value.strip().strip("'\"").strip()
    return limpio or None


def _int_env(env: Mapping[str, str], clave: str, defecto: int) -> int:
    crudo = _clean(env.get(clave))
    if not crudo:
        return defecto
    try:
        return int(crudo)
    except ValueError as exc:
        raise ConfigError(f"{clave} debe ser un número entero, no {crudo!r}") from exc


def load_watchlists(path: Path) -> WatchlistsConfig | None:
    """Carga las watchlists del usuario, o ``None`` si no hay fichero.

    No tener watchlists es un caso normal, no un error: el servidor funciona
    igual y ordena por fecha. Lo que sí es un error es un fichero que existe
    pero está mal escrito, porque el usuario cree que está vigilando algo que no
    vigila.
    """
    if not path.is_file():
        return None

    try:
        crudo = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} no es YAML válido: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"no se pudo leer {path}: {exc}") from exc

    if crudo is None:
        return None
    if not isinstance(crudo, dict):
        raise ConfigError(f"{path} debe contener un mapa de claves, no {type(crudo).__name__}")

    try:
        return WatchlistsConfig.model_validate(crudo)
    except ValidationError as exc:
        detalles = "; ".join(
            f"{'.'.join(str(parte) for parte in error['loc']) or 'raíz'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigError(f"{path} tiene errores: {detalles}") from exc
