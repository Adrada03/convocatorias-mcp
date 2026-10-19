"""Tests del comando doctor.

Dos cosas que verificar de verdad: que distingue un problema que impide
funcionar (código de salida 1) de un aviso (código 0), y que **la salida no
contiene nunca una credencial ni un dato personal**, porque está pensada para
pegarse en un issue.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from convocatorias_mcp.cli import (
    build_parser,
    check_boe,
    check_index,
    check_python,
    check_watchlists,
    main,
    run_doctor,
    run_ingest,
)
from convocatorias_mcp.config import Settings
from convocatorias_mcp.db import open_index

SUMARIO_URL = "https://www.boe.es/datosabiertos/api/boe/sumario/20261019"


WATCHLISTS_VALIDO = """
watchlists:
  - nombre: becas-informatica
    keywords: [informática, software]
    secciones: [III, V.B]
"""


def settings_para_test(tmp_path: Path, **overrides: object) -> Settings:
    data: dict[str, object] = {
        "watchlists_path": tmp_path / "watchlists.yaml",
        "index_path": tmp_path / "indice.sqlite3",
    }
    data.update(overrides)
    return Settings(**data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Comprobaciones sueltas
# --------------------------------------------------------------------------- #


def test_la_version_de_python_de_la_suite_es_suficiente():
    assert check_python().status == "ok"


def test_sin_watchlists_solo_avisa(tmp_path: Path):
    check = check_watchlists(settings_para_test(tmp_path))
    assert check.status == "warn"
    assert "fecha" in check.detail


def test_un_watchlists_roto_es_un_fallo_no_un_aviso(tmp_path: Path):
    ruta = tmp_path / "watchlists.yaml"
    ruta.write_text("watchlists:\n  - nombre: becas\n    secciones: [XIV]\n", encoding="utf-8")
    assert check_watchlists(settings_para_test(tmp_path, watchlists_path=ruta)).status == "fail"


def test_las_watchlists_se_resumen_con_recuentos_no_con_keywords(tmp_path: Path):
    ruta = tmp_path / "watchlists.yaml"
    ruta.write_text(WATCHLISTS_VALIDO, encoding="utf-8")

    check = check_watchlists(settings_para_test(tmp_path, watchlists_path=ruta))

    assert check.status == "ok"
    assert "becas-informatica" in check.detail
    assert "2 keywords" in check.detail
    # Las keywords concretas no se imprimen: dicen bastante del usuario.
    assert "software" not in check.detail


def test_un_indice_nuevo_se_crea_y_se_avisa_de_que_esta_vacio(tmp_path: Path):
    checks = check_index(settings_para_test(tmp_path))

    assert checks[0].status == "ok"
    assert checks[1].status == "warn"
    assert "ingest" in checks[1].detail
    assert (tmp_path / "indice.sqlite3").is_file()


def test_el_indice_con_contenido_se_resume(tmp_path: Path):
    ruta = tmp_path / "indice.sqlite3"
    conn = open_index(ruta)
    conn.execute(
        "INSERT INTO source_health (fuente, fecha, estado, items, ingerido_at) "
        "VALUES ('boe', '2026-10-17', 'ok', 270, '2026-10-19T10:00:00+00:00')"
    )
    conn.close()

    checks = check_index(settings_para_test(tmp_path, index_path=ruta))

    assert checks[1].status == "ok"
    assert "2026-10-17" in checks[1].detail


def test_los_huecos_del_indice_se_senalan(tmp_path: Path):
    ruta = tmp_path / "indice.sqlite3"
    conn = open_index(ruta)
    for fecha in ("2026-10-15", "2026-10-19"):
        conn.execute(
            "INSERT INTO source_health (fuente, fecha, estado, items, ingerido_at) "
            "VALUES ('boe', ?, 'ok', 1, '2026-10-19T10:00:00+00:00')",
            (fecha,),
        )
    conn.close()

    checks = check_index(settings_para_test(tmp_path, index_path=ruta))

    huecos = [check for check in checks if check.name == "huecos"]
    assert huecos and huecos[0].status == "warn"
    assert "2026-10-16" in huecos[0].detail


# --------------------------------------------------------------------------- #
# Conectividad con el BOE
# --------------------------------------------------------------------------- #


@respx.mock
def test_check_boe_confirma_que_la_api_responde():
    respx.get(SUMARIO_URL).mock(return_value=httpx.Response(200, json={"data": {}}))

    checks = check_boe(hoy=date(2026, 10, 19))

    assert checks[0].status == "ok"
    assert "HTTP 200" in checks[0].detail


@respx.mock
def test_un_404_del_boe_cuenta_como_exito():
    # Es lo que pasa un domingo: la API responde y dice que no hubo boletin.
    respx.get(SUMARIO_URL).mock(return_value=httpx.Response(404, text="<response/>"))

    checks = check_boe(hoy=date(2026, 10, 19))

    assert checks[0].status == "ok"
    assert "no hubo publicación" in checks[0].detail


@respx.mock
def test_un_5xx_del_boe_si_es_un_fallo():
    respx.get(SUMARIO_URL).mock(return_value=httpx.Response(503))

    assert check_boe(hoy=date(2026, 10, 19))[0].status == "fail"


@respx.mock
def test_un_fallo_de_red_con_el_boe_se_explica():
    respx.get(SUMARIO_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))

    checks = check_boe(hoy=date(2026, 10, 19))

    assert checks[0].status == "fail"
    assert "fallo de red" in checks[0].detail


@respx.mock
def test_check_boe_manda_el_user_agent_del_proyecto():
    ruta = respx.get(SUMARIO_URL).mock(return_value=httpx.Response(200, json={}))

    check_boe(hoy=date(2026, 10, 19))

    assert ruta.calls.last.request.headers["user-agent"].startswith("convocatorias-mcp/")


# --------------------------------------------------------------------------- #
# El comando completo
# --------------------------------------------------------------------------- #


@pytest.fixture
def entorno_limpio(tmp_path: Path, monkeypatch):
    """Aísla el doctor: índice y watchlists dentro de tmp_path."""
    monkeypatch.setenv("CONVOCATORIAS_INDEX_PATH", str(tmp_path / "indice.sqlite3"))
    monkeypatch.setenv("CONVOCATORIAS_WATCHLISTS_PATH", str(tmp_path / "watchlists.yaml"))
    monkeypatch.delenv("CONVOCATORIAS_BACKFILL_DIAS", raising=False)
    return tmp_path


def test_doctor_sin_nada_configurado_avisa_pero_no_falla(entorno_limpio: Path, capsys):
    codigo = run_doctor(cwd=entorno_limpio)

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "aviso" in salida
    assert "ingest" in salida


def test_doctor_falla_si_las_watchlists_estan_rotas(entorno_limpio: Path, capsys):
    (entorno_limpio / "watchlists.yaml").write_text("watchlists: [\n", encoding="utf-8")

    codigo = run_doctor(cwd=entorno_limpio)

    assert codigo == 1
    assert "impiden funcionar" in capsys.readouterr().out


def test_doctor_falla_con_una_configuracion_invalida(entorno_limpio: Path, capsys, monkeypatch):
    monkeypatch.setenv("CONVOCATORIAS_BACKFILL_DIAS", "tres meses")

    codigo = run_doctor(cwd=entorno_limpio)

    assert codigo == 1
    assert "CONVOCATORIAS_BACKFILL_DIAS" in capsys.readouterr().out


def test_doctor_no_sale_a_la_red_sin_check_api(entorno_limpio: Path, capsys):
    # respx sin rutas: cualquier peticion HTTP haria fallar el test.
    with respx.mock:
        codigo = run_doctor(cwd=entorno_limpio)

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "--check-api" in salida


def test_doctor_resume_sin_volcar_rutas_sensibles(entorno_limpio: Path, capsys):
    # El doctor sigue siendo la salida que un usuario pega en un issue.
    (entorno_limpio / ".env").write_text("CONVOCATORIAS_BACKFILL_DIAS=30\n", encoding="utf-8")

    assert run_doctor(cwd=entorno_limpio) == 0
    assert ".env" in capsys.readouterr().out


def test_el_parser_acepta_doctor_y_check_api():
    args = build_parser().parse_args(["doctor", "--check-api"])
    assert args.command == "doctor"
    assert args.check_api is True


def test_sin_subcomando_se_arranca_el_servidor_mcp(monkeypatch):
    # Es como lo invoca Claude Desktop. No se arranca de verdad: el transporte
    # stdio se quedaria esperando mensajes.
    arrancado = []
    monkeypatch.setattr(
        "convocatorias_mcp.server.main", lambda: arrancado.append(True) or 0, raising=True
    )

    assert main([]) == 0
    assert arrancado == [True]


def test_el_parser_acepta_ingest_con_sus_opciones():
    args = build_parser().parse_args(["ingest", "--backfill", "30", "--forzar"])
    assert args.command == "ingest"
    assert args.backfill == 30
    assert args.forzar is True


# --------------------------------------------------------------------------- #
# El comando ingest
# --------------------------------------------------------------------------- #


def test_ingest_puebla_el_indice(entorno_limpio: Path, capsys):
    codigo = run_ingest(fuente="fixture", backfill=30)

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "271 convocatorias" in salida
    assert "sin publicación" in salida
    assert (entorno_limpio / "indice.sqlite3").is_file()


def test_ingest_es_incremental_en_la_segunda_pasada(entorno_limpio: Path, capsys):
    run_ingest(fuente="fixture", backfill=30)
    capsys.readouterr()

    codigo = run_ingest(fuente="fixture", backfill=30)

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "0 convocatorias de 0 días pedidos" in salida
    assert "días ya ingeridos" in salida


def test_ingest_avisa_de_una_configuracion_invalida(entorno_limpio: Path, capsys, monkeypatch):
    monkeypatch.setenv("CONVOCATORIAS_BACKFILL_DIAS", "un mes")

    assert run_ingest(fuente="fixture") == 1
    assert "Configuración inválida" in capsys.readouterr().err
