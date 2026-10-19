"""Configuración del servidor: variables de entorno y carga del perfil.

Todo lo que cambia entre instalaciones entra por aquí, y nada de esto se
versiona. Las credenciales se leen de variables de entorno o de un ``.env``
local (ignorado por git); el perfil, de un ``profile.yaml`` propio de cada
usuario.

Regla que se cumple en todo el módulo: **el valor de un secreto nunca se
devuelve en un mensaje, un log ni un ``repr``.** :class:`Settings` lo guarda
para usarlo en las peticiones, pero lo que se puede imprimir es si está o no
configurado, y como mucho su longitud.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Self

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobscout_mcp.models import UserProfile

__all__ = [
    "DEFAULT_CACHE_TTL_SECONDS",
    "ConfigError",
    "Settings",
    "default_data_dir",
    "load_profile",
]

#: Seis horas. Con 250 peticiones diarias en el plan gratuito de Adzuna, la
#: caché no es una optimización sino parte del funcionamiento normal.
DEFAULT_CACHE_TTL_SECONDS = 6 * 60 * 60

DEFAULT_CACHE_FILENAME = "jobscout-cache.sqlite3"
DEFAULT_PROFILE_FILENAME = "profile.yaml"


class ConfigError(Exception):
    """Configuración inválida, con un mensaje que el usuario pueda accionar."""


def default_data_dir() -> Path:
    """Carpeta de datos del usuario, según la convención de cada sistema.

    El servidor lo arranca Claude Desktop, así que el directorio de trabajo es
    impredecible: la caché no puede vivir en una ruta relativa.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / "jobscout-mcp"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "jobscout-mcp"
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "jobscout-mcp"


class Settings(BaseModel):
    """Ajustes efectivos del servidor, ya resueltos.

    Se construye con :meth:`from_env`, que es el único sitio que lee el
    entorno. El resto del código recibe un ``Settings`` ya resuelto, lo que
    hace los tests triviales: se instancia a mano y no hace falta tocar
    ``os.environ``.
    """

    model_config = ConfigDict(extra="forbid")

    adzuna_app_id: str | None = Field(default=None, repr=False)
    adzuna_app_key: str | None = Field(default=None, repr=False)
    adzuna_country: str = Field(default="es", min_length=2, max_length=2)

    profile_path: Path
    cache_path: Path
    cache_ttl_seconds: int = Field(default=DEFAULT_CACHE_TTL_SECONDS, ge=0)

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

        profile_path = env.get("JOBSCOUT_PROFILE_PATH")
        if profile_path:
            resolved_profile = Path(profile_path).expanduser()
        else:
            # Un profile.yaml junto al proyecto gana al de la carpeta de datos:
            # es lo que espera quien clona el repo y copia la plantilla.
            local = cwd / DEFAULT_PROFILE_FILENAME
            resolved_profile = local if local.is_file() else data_dir / DEFAULT_PROFILE_FILENAME

        cache_path = env.get("JOBSCOUT_CACHE_PATH")
        resolved_cache = (
            Path(cache_path).expanduser() if cache_path else data_dir / DEFAULT_CACHE_FILENAME
        )

        ttl_raw = env.get("JOBSCOUT_CACHE_TTL")
        try:
            ttl = int(ttl_raw) if ttl_raw else DEFAULT_CACHE_TTL_SECONDS
        except ValueError as exc:
            raise ConfigError(
                f"JOBSCOUT_CACHE_TTL debe ser un número de segundos, no {ttl_raw!r}"
            ) from exc

        country = (env.get("ADZUNA_COUNTRY") or "es").strip().lower()
        if len(country) != 2:
            raise ConfigError(
                f"ADZUNA_COUNTRY debe ser un código de dos letras como 'es', no {country!r}"
            )

        return cls(
            adzuna_app_id=_clean_secret(env.get("ADZUNA_APP_ID")),
            adzuna_app_key=_clean_secret(env.get("ADZUNA_APP_KEY")),
            adzuna_country=country,
            profile_path=resolved_profile,
            cache_path=resolved_cache,
            cache_ttl_seconds=ttl,
        )

    @property
    def has_adzuna_credentials(self) -> bool:
        """``True`` si hay ``app_id`` y ``app_key``, sin revelar su valor."""
        return bool(self.adzuna_app_id and self.adzuna_app_key)

    def describe_secret(self, name: str) -> str:
        """Describe un secreto para el diagnóstico, sin imprimirlo.

        Devuelve solo su longitud, que basta para distinguir "no lo he puesto"
        de "lo he pegado mal" sin que el valor acabe en una captura de pantalla
        ni en el historial de la terminal.
        """
        value = getattr(self, name, None)
        if not value:
            return "no configurada"
        return f"configurada ({len(value)} caracteres)"


def _clean_secret(value: str | None) -> str | None:
    """Limpia espacios y comillas que se cuelan al pegar una clave en un .env."""
    if value is None:
        return None
    cleaned = value.strip().strip("'\"").strip()
    return cleaned or None


def load_profile(path: Path) -> UserProfile | None:
    """Carga el perfil del usuario, o ``None`` si no hay fichero.

    No tener perfil es un caso normal, no un error: el servidor funciona igual
    y ordena por fecha. Lo que sí es un error es un perfil que existe pero está
    mal escrito, porque el usuario cree que está rankeando y no lo está.
    """
    if not path.is_file():
        return None

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} no es YAML válido: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"no se pudo leer {path}: {exc}") from exc

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} debe contener un mapa de claves, no {type(raw).__name__}")

    try:
        return UserProfile.model_validate(raw)
    except ValidationError as exc:
        detalles = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'raíz'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigError(f"{path} tiene errores: {detalles}") from exc
