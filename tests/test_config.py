"""Tests de la configuración.

Lo que más importa aquí no es que lea bien las variables, sino que **ni un
secreto ni un dato personal se escapen** por un ``repr`` o un mensaje de error,
y que un ``watchlists.yaml`` mal escrito avise en vez de ignorarse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from convocatorias_mcp.config import (
    DEFAULT_BACKFILL_DIAS,
    TZ_BOLETIN,
    ConfigError,
    Settings,
    default_data_dir,
    hoy_boletin,
    load_watchlists,
)
from convocatorias_mcp.models import Seccion

WATCHLISTS_VALIDO = """
watchlists:
  - nombre: becas-informatica
    keywords: [informática, software, telecomunicaciones]
    secciones: [III, V.B]
    min_score: 0.5
  - nombre: empleo-publico-tic
    keywords: [analista, programador]
    secciones: [II.B]
    ambito: [Madrid, Estatal]
notificacion:
  avisar_plazo_dias: 5
"""

CONTRASENA = "contrasena-de-aplicacion-123"
EMAIL = "alguien@example.com"


def settings_para_test(tmp_path: Path, **overrides: object) -> Settings:
    data: dict[str, object] = {
        "watchlists_path": tmp_path / "watchlists.yaml",
        "index_path": tmp_path / "indice.sqlite3",
    }
    data.update(overrides)
    return Settings(**data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Zona horaria
# --------------------------------------------------------------------------- #


def test_la_zona_horaria_es_la_del_boletin():
    # El BOE publica en hora espanola: "hoy" no puede depender del servidor.
    assert str(TZ_BOLETIN) == "Europe/Madrid"


def test_hoy_boletin_devuelve_una_fecha():
    assert hoy_boletin().year >= 2024


# --------------------------------------------------------------------------- #
# Lectura del entorno
# --------------------------------------------------------------------------- #


def test_valores_por_defecto(tmp_path: Path):
    settings = Settings.from_env({}, cwd=tmp_path)
    assert settings.backfill_dias == DEFAULT_BACKFILL_DIAS
    assert settings.index_path.parent == default_data_dir()
    assert not settings.has_smtp


def test_lee_la_configuracion_de_smtp(tmp_path: Path):
    settings = Settings.from_env(
        {
            "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PORT": "465",
            "SMTP_USER": "yo@gmail.com",
            "SMTP_PASSWORD": CONTRASENA,
            "DIGEST_TO": EMAIL,
        },
        cwd=tmp_path,
    )
    assert settings.smtp_host == "smtp.gmail.com"
    assert settings.smtp_port == 465
    assert settings.has_smtp


def test_smtp_a_medias_no_cuenta_como_configurado(tmp_path: Path):
    settings = Settings.from_env({"SMTP_HOST": "smtp.gmail.com"}, cwd=tmp_path)
    assert not settings.has_smtp


def test_lee_el_dotenv_cuando_el_entorno_no_trae_el_valor(tmp_path: Path):
    (tmp_path / ".env").write_text(f"SMTP_PASSWORD={CONTRASENA}\n", encoding="utf-8")
    assert Settings.from_env({}, cwd=tmp_path).smtp_password == CONTRASENA


def test_el_entorno_real_gana_al_dotenv(tmp_path: Path):
    (tmp_path / ".env").write_text("SMTP_HOST=desde-dotenv\n", encoding="utf-8")
    settings = Settings.from_env({"SMTP_HOST": "desde-entorno"}, cwd=tmp_path)
    assert settings.smtp_host == "desde-entorno"


@pytest.mark.parametrize("crudo", ['"secreto"', "'secreto'", "  secreto  "])
def test_limpia_comillas_y_espacios_al_pegar_un_valor(tmp_path: Path, crudo: str):
    assert Settings.from_env({"SMTP_PASSWORD": crudo}, cwd=tmp_path).smtp_password == "secreto"


def test_un_valor_vacio_es_como_no_tenerlo(tmp_path: Path):
    assert Settings.from_env({"SMTP_PASSWORD": "   "}, cwd=tmp_path).smtp_password is None


def test_un_backfill_no_numerico_da_un_error_accionable(tmp_path: Path):
    with pytest.raises(ConfigError, match="CONVOCATORIAS_BACKFILL_DIAS"):
        Settings.from_env({"CONVOCATORIAS_BACKFILL_DIAS": "tres meses"}, cwd=tmp_path)


def test_un_puerto_no_numerico_da_un_error_accionable(tmp_path: Path):
    with pytest.raises(ConfigError, match="SMTP_PORT"):
        Settings.from_env({"SMTP_PORT": "quinientos"}, cwd=tmp_path)


def test_el_watchlists_local_gana_al_de_la_carpeta_de_datos(tmp_path: Path):
    local = tmp_path / "watchlists.yaml"
    local.write_text(WATCHLISTS_VALIDO, encoding="utf-8")
    assert Settings.from_env({}, cwd=tmp_path).watchlists_path == local


def test_sin_watchlists_local_se_usa_la_carpeta_de_datos(tmp_path: Path):
    assert Settings.from_env({}, cwd=tmp_path).watchlists_path.parent == default_data_dir()


def test_las_rutas_se_pueden_forzar_por_entorno(tmp_path: Path):
    settings = Settings.from_env(
        {
            "CONVOCATORIAS_WATCHLISTS_PATH": str(tmp_path / "otras.yaml"),
            "CONVOCATORIAS_INDEX_PATH": str(tmp_path / "otro.sqlite3"),
            "CONVOCATORIAS_BACKFILL_DIAS": "30",
        },
        cwd=tmp_path,
    )
    assert settings.watchlists_path == tmp_path / "otras.yaml"
    assert settings.index_path == tmp_path / "otro.sqlite3"
    assert settings.backfill_dias == 30


def test_la_carpeta_de_datos_es_absoluta():
    assert default_data_dir().is_absolute()


# --------------------------------------------------------------------------- #
# Nada sensible se escapa
# --------------------------------------------------------------------------- #


def test_el_repr_no_revela_la_contrasena_ni_el_destinatario(tmp_path: Path):
    settings = settings_para_test(
        tmp_path, smtp_password=CONTRASENA, digest_to=EMAIL, smtp_user="yo@gmail.com"
    )
    texto = repr(settings)
    assert CONTRASENA not in texto
    assert EMAIL not in texto
    assert "yo@gmail.com" not in texto


def test_describe_secret_solo_dice_la_longitud(tmp_path: Path):
    settings = settings_para_test(tmp_path, smtp_password=CONTRASENA, digest_to=EMAIL)
    descripcion = settings.describe_secret("smtp_password")
    assert CONTRASENA not in descripcion
    assert f"{len(CONTRASENA)} caracteres" in descripcion
    # El email tampoco se imprime, ni enmascarado.
    assert EMAIL not in settings.describe_secret("digest_to")
    assert settings.describe_secret("smtp_host") == "no configurado"


# --------------------------------------------------------------------------- #
# Carga de watchlists
# --------------------------------------------------------------------------- #


def test_sin_fichero_no_hay_watchlists_y_no_es_un_error(tmp_path: Path):
    assert load_watchlists(tmp_path / "no-existe.yaml") is None


def test_carga_un_fichero_valido(tmp_path: Path):
    ruta = tmp_path / "watchlists.yaml"
    ruta.write_text(WATCHLISTS_VALIDO, encoding="utf-8")

    config = load_watchlists(ruta)

    assert config is not None
    assert len(config.watchlists) == 2
    becas = config.get("becas-informatica")
    assert becas is not None
    assert becas.secciones == [Seccion.III, Seccion.V_B]
    assert becas.keywords == ["informatica", "software", "telecomunicaciones"]
    assert becas.min_score == 0.5
    assert config.notificacion.avisar_plazo_dias == 5


def test_un_fichero_vacio_se_trata_como_ausente(tmp_path: Path):
    ruta = tmp_path / "watchlists.yaml"
    ruta.write_text("# todo comentado\n", encoding="utf-8")
    assert load_watchlists(ruta) is None


def test_un_yaml_roto_avisa(tmp_path: Path):
    ruta = tmp_path / "watchlists.yaml"
    ruta.write_text("watchlists: [\n  - nombre: becas\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no es YAML válido"):
        load_watchlists(ruta)


def test_un_fichero_que_no_es_un_mapa_avisa(tmp_path: Path):
    ruta = tmp_path / "watchlists.yaml"
    ruta.write_text("- becas\n- empleo\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapa de claves"):
        load_watchlists(ruta)


def test_un_typo_en_la_clave_avisa_en_vez_de_ignorarse(tmp_path: Path):
    # El fallo que importa: el usuario cree que esta vigilando y no lo esta.
    ruta = tmp_path / "watchlists.yaml"
    ruta.write_text("watchlist:\n  - nombre: becas\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_watchlists(ruta)
    assert "watchlist" in str(exc_info.value)


def test_una_seccion_mal_escrita_avisa_con_la_ruta_del_error(tmp_path: Path):
    ruta = tmp_path / "watchlists.yaml"
    ruta.write_text("watchlists:\n  - nombre: becas\n    secciones: [XIV]\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_watchlists(ruta)
    mensaje = str(exc_info.value)
    assert "secciones" in mensaje
    assert "II.B" in mensaje


def test_dos_watchlists_con_el_mismo_nombre_avisan(tmp_path: Path):
    ruta = tmp_path / "watchlists.yaml"
    ruta.write_text("watchlists:\n  - nombre: becas\n  - nombre: Becas\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="dos watchlists"):
        load_watchlists(ruta)


def test_el_ejemplo_versionado_es_valido():
    # Si la plantilla del repo no carga, el primer arranque de cualquiera falla.
    ruta = Path(__file__).resolve().parent.parent / "watchlists.example.yaml"
    config = load_watchlists(ruta)
    assert config is not None
    assert not config.is_empty
