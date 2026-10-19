"""Tests del comando doctor.

Dos cosas que verificar de verdad: que distingue un problema que impide
funcionar (código de salida 1) de un aviso (código 0), y que **la salida no
contiene nunca una credencial**, porque está pensada para pegarse en un issue.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from jobscout_mcp.cli import (
    build_parser,
    check_adzuna_api,
    check_dotenv,
    check_profile,
    check_python,
    main,
    run_doctor,
)
from jobscout_mcp.config import Settings

SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/es/search/1"

RESPUESTA_ADZUNA = {
    "count": 1234,
    "mean": 42000.0,
    "results": [
        {
            "id": "4001181459",
            "title": "Backend Python Developer",
            "description": "Buscamos backend...",
            "created": "2026-10-17T09:12:34Z",
            "redirect_url": "https://www.adzuna.es/details/4001181459",
            "salary_min": 33000,
            "salary_max": 39000,
            "salary_is_predicted": "0",
            "contract_type": "permanent",
            "contract_time": "full_time",
            "company": {"display_name": "Acme"},
            "location": {"display_name": "Madrid", "area": ["España", "Madrid"]},
            "category": {"label": "IT Jobs", "tag": "it-jobs"},
        }
    ],
}

CLAVE = "clave-secretisima-123"


def settings_con_credenciales(tmp_path: Path) -> Settings:
    return Settings(
        adzuna_app_id="id-secreto-456",
        adzuna_app_key=CLAVE,
        profile_path=tmp_path / "profile.yaml",
        cache_path=tmp_path / "cache.sqlite3",
    )


# --------------------------------------------------------------------------- #
# Comprobaciones sueltas
# --------------------------------------------------------------------------- #


def test_la_version_de_python_de_la_suite_es_suficiente():
    assert check_python().status == "ok"


def test_avisa_si_falta_el_dotenv(tmp_path: Path):
    check = check_dotenv(tmp_path)
    assert check.status == "warn"
    assert ".env.example" in check.detail


def test_detecta_el_dotenv(tmp_path: Path):
    (tmp_path / ".env").write_text("ADZUNA_APP_ID=x\n", encoding="utf-8")
    assert check_dotenv(tmp_path).status == "ok"


def test_un_perfil_roto_es_un_fallo_no_un_aviso(tmp_path: Path):
    ruta = tmp_path / "profile.yaml"
    ruta.write_text("skills: [python\n", encoding="utf-8")
    settings = Settings(profile_path=ruta, cache_path=tmp_path / "cache.sqlite3")
    assert check_profile(settings).status == "fail"


def test_sin_perfil_solo_avisa(tmp_path: Path):
    settings = Settings(
        profile_path=tmp_path / "no-existe.yaml", cache_path=tmp_path / "cache.sqlite3"
    )
    check = check_profile(settings)
    assert check.status == "warn"
    assert "fecha" in check.detail


def test_el_perfil_se_resume_con_recuentos_no_con_contenidos(tmp_path: Path):
    ruta = tmp_path / "profile.yaml"
    ruta.write_text("skills: [python, fastapi]\nseniority: junior\n", encoding="utf-8")
    settings = Settings(profile_path=ruta, cache_path=tmp_path / "cache.sqlite3")
    check = check_profile(settings)
    assert check.status == "ok"
    assert "2 skills" in check.detail
    # Las tecnologías concretas no se imprimen.
    assert "fastapi" not in check.detail


# --------------------------------------------------------------------------- #
# La comprobación real de la API
# --------------------------------------------------------------------------- #


@respx.mock
def test_check_api_confirma_el_pais_y_los_campos(tmp_path: Path):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=RESPUESTA_ADZUNA))

    checks = check_adzuna_api(settings_con_credenciales(tmp_path))

    assert [check.status for check in checks] == ["ok", "ok"]
    assert "1234 ofertas" in checks[0].detail
    assert "'es'" in checks[0].detail
    assert "redirect_url" in checks[1].detail


@respx.mock
def test_check_api_no_filtra_la_clave_en_la_salida(tmp_path: Path):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=RESPUESTA_ADZUNA))

    checks = check_adzuna_api(settings_con_credenciales(tmp_path))

    for check in checks:
        assert CLAVE not in check.render()
        assert "id-secreto-456" not in check.render()


@respx.mock
def test_check_api_manda_el_user_agent_del_proyecto(tmp_path: Path):
    ruta = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=RESPUESTA_ADZUNA))

    check_adzuna_api(settings_con_credenciales(tmp_path))

    assert ruta.calls.last.request.headers["user-agent"].startswith("jobscout-mcp/")


@respx.mock
def test_check_api_pide_una_sola_oferta_para_gastar_lo_minimo(tmp_path: Path):
    ruta = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=RESPUESTA_ADZUNA))

    check_adzuna_api(settings_con_credenciales(tmp_path))

    assert ruta.calls.last.request.url.params["results_per_page"] == "1"


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_check_api_distingue_credenciales_rechazadas(tmp_path: Path, status: int):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(status))

    checks = check_adzuna_api(settings_con_credenciales(tmp_path))

    assert checks[0].status == "fail"
    assert "credenciales rechazadas" in checks[0].detail


@respx.mock
def test_check_api_explica_un_400_con_html(tmp_path: Path):
    # Es lo que devuelve Adzuna de verdad cuando algo no cuadra: HTML, no JSON.
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(400, html="<html>Uh oh, something isn't right</html>")
    )

    checks = check_adzuna_api(settings_con_credenciales(tmp_path))

    assert checks[0].status == "fail"
    assert "país 'es'" in checks[0].detail


@respx.mock
def test_check_api_avisa_si_el_pais_no_devuelve_resultados(tmp_path: Path):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"count": 0, "results": []}))

    checks = check_adzuna_api(settings_con_credenciales(tmp_path))

    assert checks[0].status == "ok"
    assert checks[1].status == "warn"
    assert "cobertura" in checks[1].detail


@respx.mock
def test_check_api_convierte_un_fallo_de_red_en_fallo_legible(tmp_path: Path):
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))

    checks = check_adzuna_api(settings_con_credenciales(tmp_path))

    assert checks[0].status == "fail"
    assert "fallo de red" in checks[0].detail


def test_check_api_se_omite_sin_credenciales(tmp_path: Path):
    settings = Settings(profile_path=tmp_path / "p.yaml", cache_path=tmp_path / "c.sqlite3")

    checks = check_adzuna_api(settings)

    assert checks[0].status == "warn"
    assert "faltan credenciales" in checks[0].detail


# --------------------------------------------------------------------------- #
# El comando completo
# --------------------------------------------------------------------------- #


def test_doctor_sin_nada_configurado_avisa_pero_no_falla(tmp_path: Path, capsys):
    codigo = run_doctor(cwd=tmp_path)

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "aviso" in salida
    assert "developer.adzuna.com/signup" in salida


def test_doctor_falla_si_el_perfil_esta_roto(tmp_path: Path, capsys):
    (tmp_path / "profile.yaml").write_text("skills: [python\n", encoding="utf-8")

    codigo = run_doctor(cwd=tmp_path)

    assert codigo == 1
    assert "impiden funcionar" in capsys.readouterr().out


def test_doctor_falla_con_una_configuracion_invalida(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("JOBSCOUT_CACHE_TTL", "un rato")

    codigo = run_doctor(cwd=tmp_path)

    assert codigo == 1
    assert "JOBSCOUT_CACHE_TTL" in capsys.readouterr().out


def test_doctor_no_sale_a_la_red_sin_check_api(tmp_path: Path, capsys):
    (tmp_path / ".env").write_text(
        f"ADZUNA_APP_ID=id-secreto-456\nADZUNA_APP_KEY={CLAVE}\n", encoding="utf-8"
    )

    # respx sin rutas: cualquier petición HTTP haría fallar el test.
    with respx.mock:
        codigo = run_doctor(cwd=tmp_path)

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "--check-api" in salida


def test_doctor_nunca_imprime_las_credenciales(tmp_path: Path, capsys):
    (tmp_path / ".env").write_text(
        f"ADZUNA_APP_ID=id-secreto-456\nADZUNA_APP_KEY={CLAVE}\n", encoding="utf-8"
    )

    run_doctor(cwd=tmp_path)

    salida = capsys.readouterr().out
    assert CLAVE not in salida
    assert "id-secreto-456" not in salida
    # Pero sí confirma que están puestas, con su longitud.
    assert f"({len(CLAVE)} caracteres)" in salida


def test_el_parser_acepta_doctor_y_check_api():
    args = build_parser().parse_args(["doctor", "--check-api"])
    assert args.command == "doctor"
    assert args.check_api is True


def test_sin_subcomando_avisa_de_que_el_servidor_no_esta_listo(capsys):
    codigo = main([])

    assert codigo == 2
    assert "doctor" in capsys.readouterr().err
