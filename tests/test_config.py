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


def test_lee_el_dotenv_cuando_el_entorno_no_trae_el_valor(tmp_path: Path):
    (tmp_path / ".env").write_text("CONVOCATORIAS_BACKFILL_DIAS=15\n", encoding="utf-8")
    assert Settings.from_env({}, cwd=tmp_path).backfill_dias == 15


def test_el_entorno_real_gana_al_dotenv(tmp_path: Path):
    (tmp_path / ".env").write_text("CONVOCATORIAS_BACKFILL_DIAS=15\n", encoding="utf-8")
    ajustes = Settings.from_env({"CONVOCATORIAS_BACKFILL_DIAS": "40"}, cwd=tmp_path)
    assert ajustes.backfill_dias == 40


@pytest.mark.parametrize("crudo", ['"30"', "'30'", "  30  "])
def test_limpia_comillas_y_espacios_al_pegar_un_valor(tmp_path: Path, crudo: str):
    ajustes = Settings.from_env({"CONVOCATORIAS_BACKFILL_DIAS": crudo}, cwd=tmp_path)
    assert ajustes.backfill_dias == 30


def test_un_backfill_no_numerico_da_un_error_accionable(tmp_path: Path):
    with pytest.raises(ConfigError, match="CONVOCATORIAS_BACKFILL_DIAS"):
        Settings.from_env({"CONVOCATORIAS_BACKFILL_DIAS": "tres meses"}, cwd=tmp_path)


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


def test_describe_secret_no_revela_el_valor(tmp_path: Path):
    # Sigue habiendo un sitio que describe secretos sin imprimirlos: cuando
    # entre una fuente con credenciales, esta es la herramienta.
    settings = settings_para_test(tmp_path)
    assert settings.describe_secret("index_path").startswith("configurado (")
    assert settings.describe_secret("no_existe") == "no configurado"


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


def test_las_watchlists_de_demostracion_cargan_y_avisan():
    """El fichero que usa el dashboard publicado.

    Si no cargara, el sitio saldria sin ranking; si no llevara nota, no
    quedaria claro que sus intereses son inventados y no los de nadie.
    """
    ruta = Path(__file__).resolve().parent.parent / "watchlists.demo.yaml"

    config = load_watchlists(ruta)

    assert config is not None
    assert len(config.watchlists) == 4
    assert config.nota is not None
    assert "de ejemplo" in config.nota
    assert "watchlists.yaml" in config.nota
