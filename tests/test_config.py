"""Tests de la configuración.

Lo que más importa aquí no es que lea bien las variables, sino que **un
secreto no se escape nunca** por un ``repr`` o un mensaje de error, y que un
``profile.yaml`` mal escrito avise en vez de ignorarse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobscout_mcp.config import (
    DEFAULT_CACHE_TTL_SECONDS,
    ConfigError,
    Settings,
    default_data_dir,
    load_profile,
)
from jobscout_mcp.models import Seniority

PERFIL_VALIDO = """
skills: [python, fastapi]
nice_to_have: [aws]
seniority: junior
locations: [Madrid, remoto]
contract_types: [internship, full_time]
exclude_keywords: [ventas]
"""


def settings_para_test(tmp_path: Path, **overrides: object) -> Settings:
    data: dict[str, object] = {
        "profile_path": tmp_path / "profile.yaml",
        "cache_path": tmp_path / "cache.sqlite3",
    }
    data.update(overrides)
    return Settings(**data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Lectura del entorno
# --------------------------------------------------------------------------- #


def test_lee_las_credenciales_del_entorno(tmp_path: Path):
    settings = Settings.from_env(
        {"ADZUNA_APP_ID": "id123", "ADZUNA_APP_KEY": "key456", "ADZUNA_COUNTRY": "ES"},
        cwd=tmp_path,
    )
    assert settings.adzuna_app_id == "id123"
    assert settings.adzuna_app_key == "key456"
    assert settings.adzuna_country == "es"  # se normaliza a minúsculas
    assert settings.has_adzuna_credentials


def test_sin_credenciales_no_hay_credenciales(tmp_path: Path):
    settings = Settings.from_env({}, cwd=tmp_path)
    assert not settings.has_adzuna_credentials
    assert settings.adzuna_country == "es"
    assert settings.cache_ttl_seconds == DEFAULT_CACHE_TTL_SECONDS


def test_lee_el_dotenv_cuando_el_entorno_no_trae_el_valor(tmp_path: Path):
    (tmp_path / ".env").write_text("ADZUNA_APP_ID=desde_dotenv\n", encoding="utf-8")
    settings = Settings.from_env({}, cwd=tmp_path)
    assert settings.adzuna_app_id == "desde_dotenv"


def test_el_entorno_real_gana_al_dotenv(tmp_path: Path):
    # Permite sobrescribir un valor puntual sin editar el fichero.
    (tmp_path / ".env").write_text("ADZUNA_APP_ID=desde_dotenv\n", encoding="utf-8")
    settings = Settings.from_env({"ADZUNA_APP_ID": "desde_entorno"}, cwd=tmp_path)
    assert settings.adzuna_app_id == "desde_entorno"


@pytest.mark.parametrize("crudo", ['"key456"', "'key456'", "  key456  ", '  "key456" '])
def test_limpia_comillas_y_espacios_al_pegar_una_clave(tmp_path: Path, crudo: str):
    settings = Settings.from_env({"ADZUNA_APP_KEY": crudo}, cwd=tmp_path)
    assert settings.adzuna_app_key == "key456"


def test_una_clave_vacia_es_como_no_tenerla(tmp_path: Path):
    settings = Settings.from_env({"ADZUNA_APP_KEY": "   "}, cwd=tmp_path)
    assert settings.adzuna_app_key is None


def test_ttl_invalido_da_un_error_accionable(tmp_path: Path):
    with pytest.raises(ConfigError, match="JOBSCOUT_CACHE_TTL"):
        Settings.from_env({"JOBSCOUT_CACHE_TTL": "seis horas"}, cwd=tmp_path)


def test_pais_invalido_da_un_error_accionable(tmp_path: Path):
    with pytest.raises(ConfigError, match="ADZUNA_COUNTRY"):
        Settings.from_env({"ADZUNA_COUNTRY": "espana"}, cwd=tmp_path)


def test_el_profile_local_gana_al_de_la_carpeta_de_datos(tmp_path: Path):
    local = tmp_path / "profile.yaml"
    local.write_text(PERFIL_VALIDO, encoding="utf-8")
    assert Settings.from_env({}, cwd=tmp_path).profile_path == local


def test_sin_profile_local_se_usa_la_carpeta_de_datos(tmp_path: Path):
    settings = Settings.from_env({}, cwd=tmp_path)
    assert settings.profile_path.parent == default_data_dir()


def test_las_rutas_se_pueden_forzar_por_entorno(tmp_path: Path):
    settings = Settings.from_env(
        {
            "JOBSCOUT_PROFILE_PATH": str(tmp_path / "otro.yaml"),
            "JOBSCOUT_CACHE_PATH": str(tmp_path / "otra.sqlite3"),
            "JOBSCOUT_CACHE_TTL": "60",
        },
        cwd=tmp_path,
    )
    assert settings.profile_path == tmp_path / "otro.yaml"
    assert settings.cache_path == tmp_path / "otra.sqlite3"
    assert settings.cache_ttl_seconds == 60


def test_la_carpeta_de_datos_es_absoluta():
    assert default_data_dir().is_absolute()


# --------------------------------------------------------------------------- #
# Los secretos no se escapan
# --------------------------------------------------------------------------- #


def test_el_repr_no_revela_los_secretos(tmp_path: Path):
    settings = settings_para_test(tmp_path, adzuna_app_id="id123", adzuna_app_key="key456")
    texto = repr(settings)
    assert "id123" not in texto
    assert "key456" not in texto


def test_describe_secret_solo_dice_la_longitud(tmp_path: Path):
    settings = settings_para_test(tmp_path, adzuna_app_key="key456")
    descripcion = settings.describe_secret("adzuna_app_key")
    assert "key456" not in descripcion
    assert "6 caracteres" in descripcion
    assert settings.describe_secret("adzuna_app_id") == "no configurada"


# --------------------------------------------------------------------------- #
# Carga del perfil
# --------------------------------------------------------------------------- #


def test_sin_fichero_de_perfil_no_hay_perfil_y_no_es_un_error(tmp_path: Path):
    assert load_profile(tmp_path / "no-existe.yaml") is None


def test_carga_un_perfil_valido(tmp_path: Path):
    ruta = tmp_path / "profile.yaml"
    ruta.write_text(PERFIL_VALIDO, encoding="utf-8")
    perfil = load_profile(ruta)
    assert perfil is not None
    assert perfil.skills == ["python", "fastapi"]
    assert perfil.seniority is Seniority.JUNIOR
    assert perfil.wants_remote


def test_un_perfil_vacio_se_trata_como_ausente(tmp_path: Path):
    ruta = tmp_path / "profile.yaml"
    ruta.write_text("# todo comentado\n", encoding="utf-8")
    assert load_profile(ruta) is None


def test_un_perfil_con_yaml_roto_avisa(tmp_path: Path):
    ruta = tmp_path / "profile.yaml"
    ruta.write_text("skills: [python\nseniority: junior\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no es YAML válido"):
        load_profile(ruta)


def test_un_perfil_que_no_es_un_mapa_avisa(tmp_path: Path):
    ruta = tmp_path / "profile.yaml"
    ruta.write_text("- python\n- fastapi\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapa de claves"):
        load_profile(ruta)


def test_un_typo_en_el_perfil_avisa_en_vez_de_ignorarse(tmp_path: Path):
    # El fallo que importa: el usuario cree que está rankeando y no lo está.
    ruta = tmp_path / "profile.yaml"
    ruta.write_text("skils: [python]\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_profile(ruta)
    assert "skils" in str(exc_info.value)


def test_un_seniority_invalido_avisa(tmp_path: Path):
    ruta = tmp_path / "profile.yaml"
    ruta.write_text("seniority: dios del olimpo\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="seniority"):
        load_profile(ruta)
