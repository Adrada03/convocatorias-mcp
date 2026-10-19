"""Tests de los modelos de dominio.

Cubren lo que el resto del proyecto da por supuesto: que la deduplicación
tolera formatos distintos, que los ids son estables, que los filtros son
permisivos con lo desconocido y que un perfil vacío no rompe nada.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from convocatorias_mcp.models import (
    ContractType,
    JobListing,
    SalaryRange,
    SearchFilters,
    SearchResult,
    Seniority,
    SourceStatus,
    UserProfile,
    WorkMode,
    dedupe_key,
    normalize_company,
    normalize_text,
)

# --------------------------------------------------------------------------- #
# Normalización y deduplicación
# --------------------------------------------------------------------------- #


def test_normalize_text_quita_acentos_puntuacion_y_espacios():
    assert normalize_text("  Diseñador   Sénior, ¡C++!  ") == "disenador senior c"


def test_normalize_company_ignora_sufijos_societarios():
    assert normalize_company("ACME S.L.") == normalize_company("Acme SL")
    assert normalize_company("Everis Spain") == "everis"


@pytest.mark.parametrize(
    ("title_a", "company_a", "title_b", "company_b"),
    [
        # Mayúsculas, espacios y acentos no deben separar la misma oferta.
        ("Backend Python", "Acme S.L.", "  backend   python ", "ACME SL"),
        ("Programador Sénior Java", "Indra", "Programador Senior Java", "indra"),
        # El ruido de modalidad en el título tampoco.
        ("DevOps Engineer (100% remoto)", "Acme", "DevOps Engineer", "Acme"),
    ],
)
def test_dedupe_key_agrupa_la_misma_oferta(title_a, company_a, title_b, company_b):
    assert dedupe_key(title_a, company_a) == dedupe_key(title_b, company_b)


def test_dedupe_key_distingue_ofertas_distintas():
    assert dedupe_key("Backend Python", "Acme") != dedupe_key("Backend Python", "Globant")
    assert dedupe_key("Backend Python", "Acme") != dedupe_key("Frontend React", "Acme")


# --------------------------------------------------------------------------- #
# JobListing
# --------------------------------------------------------------------------- #


def test_id_usa_el_id_interno_del_portal(listing_factory):
    assert listing_factory(external_id="rf-12345").id == "tecnoempleo:rf-12345"


def test_id_es_estable_sin_id_interno(listing_factory):
    primero = listing_factory(external_id=None)
    segundo = listing_factory(external_id=None)
    assert primero.id == segundo.id
    assert primero.id.startswith("tecnoempleo:")


def test_id_distingue_ofertas_del_mismo_portal(listing_factory):
    otra = listing_factory(external_id=None, source_url="https://www.tecnoempleo.com/oferta/rf-2")
    assert listing_factory(external_id=None).id != otra.id


def test_seen_in_sources_se_inicializa_con_la_fuente(listing_factory):
    assert listing_factory().seen_in_sources == ["tecnoempleo"]


def test_tags_se_limpian_y_deduplican(listing_factory):
    listing = listing_factory(tags=["Python", " python ", "", "FastAPI"])
    assert listing.tags == ["Python", "FastAPI"]


def test_completeness_premia_a_la_oferta_mas_informativa(listing_factory):
    completa = listing_factory(description="Descripción larga", salary=SalaryRange(raw="33.000€"))
    escueta = listing_factory(
        location=None,
        posted_at=None,
        excerpt=None,
        tags=[],
        work_mode=WorkMode.UNKNOWN,
        contract_type=ContractType.UNKNOWN,
        seniority=Seniority.UNKNOWN,
    )
    assert completa.completeness > escueta.completeness


def test_searchable_text_junta_todo_el_texto_normalizado(listing_factory):
    texto = listing_factory(excerpt="Con PostgreSQL y Docker").searchable_text
    assert "postgresql" in texto
    assert "backend python developer" in texto


def test_source_url_debe_ser_absoluta():
    with pytest.raises(ValidationError, match="http"):
        JobListing(
            source="tecnoempleo",
            source_url="/ofertas-trabajo/rf-1",
            title="Backend",
            company="Acme",
        )


def test_round_trip_json_preserva_la_oferta(listing_factory):
    original = listing_factory()
    recuperada = JobListing.model_validate_json(original.model_dump_json())
    assert recuperada.id == original.id
    assert recuperada.dedupe_key == original.dedupe_key
    assert recuperada.tags == original.tags
    assert recuperada.posted_at == original.posted_at


# --------------------------------------------------------------------------- #
# Enums desde texto libre
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Prácticas / Beca", ContractType.INTERNSHIP),
        ("Contrato indefinido", ContractType.FULL_TIME),
        ("Jornada parcial", ContractType.PART_TIME),
        ("Autónomo / Freelance", ContractType.FREELANCE),
        ("Temporal por obra y servicio", ContractType.TEMPORARY),
        ("Lo que sea", ContractType.UNKNOWN),
        (None, ContractType.UNKNOWN),
    ],
)
def test_contract_type_from_text(texto, esperado):
    assert ContractType.from_text(texto) is esperado


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("100% remoto", WorkMode.REMOTE),
        ("Teletrabajo", WorkMode.REMOTE),
        ("Híbrido (2 días oficina)", WorkMode.HYBRID),
        ("Presencial", WorkMode.ON_SITE),
        ("", WorkMode.UNKNOWN),
    ],
)
def test_work_mode_from_text(texto, esperado):
    assert WorkMode.from_text(texto) is esperado


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Becario de desarrollo", Seniority.INTERN),
        ("Desarrollador Junior", Seniority.JUNIOR),
        ("Backend Sénior", Seniority.SENIOR),
        ("Tech Lead", Seniority.LEAD),
        ("Desarrollador", Seniority.UNKNOWN),
    ],
)
def test_seniority_from_text(texto, esperado):
    assert Seniority.from_text(texto) is esperado


def test_seniority_rank_ordena_los_niveles():
    assert Seniority.INTERN.rank < Seniority.JUNIOR.rank < Seniority.SENIOR.rank
    assert Seniority.UNKNOWN.rank == -1


def test_work_mode_remote_friendly():
    assert WorkMode.REMOTE.is_remote_friendly
    assert WorkMode.HYBRID.is_remote_friendly
    assert not WorkMode.ON_SITE.is_remote_friendly
    assert not WorkMode.UNKNOWN.is_remote_friendly


# --------------------------------------------------------------------------- #
# SalaryRange
# --------------------------------------------------------------------------- #


def test_salary_range_ordena_los_limites_al_reves():
    salario = SalaryRange(raw="39.000€ - 33.000€", min_amount=39000, max_amount=33000)
    assert (salario.min_amount, salario.max_amount) == (33000, 39000)


# --------------------------------------------------------------------------- #
# SearchFilters
# --------------------------------------------------------------------------- #


def test_keyword_list_normaliza_y_separa():
    assert SearchFilters(keywords="  Backend   Python ").keyword_list == ["backend", "python"]


def test_filtros_rechazan_parametros_desconocidos():
    with pytest.raises(ValidationError):
        SearchFilters(keywords="python", provincia="Madrid")  # type: ignore[call-arg]


def test_limit_per_source_tiene_tope():
    with pytest.raises(ValidationError):
        SearchFilters(keywords="python", limit_per_source=500)


def test_matches_descarta_lo_no_remoto_si_se_pide_remoto(listing_factory):
    filtros = SearchFilters(keywords="python", remote=True)
    assert filtros.matches(listing_factory(work_mode=WorkMode.REMOTE))
    assert filtros.matches(listing_factory(work_mode=WorkMode.HYBRID))
    assert not filtros.matches(listing_factory(work_mode=WorkMode.ON_SITE))


def test_matches_es_permisivo_con_el_contrato_desconocido(listing_factory):
    filtros = SearchFilters(keywords="python", contract_type=ContractType.INTERNSHIP)
    assert filtros.matches(listing_factory(contract_type=ContractType.UNKNOWN))
    assert filtros.matches(listing_factory(contract_type=ContractType.INTERNSHIP))
    assert not filtros.matches(listing_factory(contract_type=ContractType.FULL_TIME))


def test_matches_aplica_la_antiguedad_maxima(listing_factory):
    filtros = SearchFilters(keywords="python", posted_within_days=7)
    hoy = date(2026, 10, 19)
    assert filtros.matches(listing_factory(posted_at=date(2026, 10, 15)), today=hoy)
    assert not filtros.matches(listing_factory(posted_at=date(2026, 9, 1)), today=hoy)
    # Sin fecha no se descarta: castigaría al portal que publica menos datos.
    assert filtros.matches(listing_factory(posted_at=None), today=hoy)


# --------------------------------------------------------------------------- #
# SearchResult y SourceStatus
# --------------------------------------------------------------------------- #


def test_source_status_distingue_ok_error_y_skipped():
    assert SourceStatus.error("timeout tras 15s") == "error: timeout tras 15s"
    assert SourceStatus.is_error(SourceStatus.error("vaya"))
    assert not SourceStatus.is_error(SourceStatus.OK)
    assert not SourceStatus.is_error(SourceStatus.SKIPPED)


def test_search_result_resume_el_estado_de_las_fuentes():
    resultado = SearchResult(
        filters=SearchFilters(keywords="python"),
        source_status={
            "tecnoempleo": SourceStatus.OK,
            "infojobs": SourceStatus.error("HTTP 503"),
            "talentoteca": SourceStatus.SKIPPED,
        },
    )
    assert resultado.ok_sources == ["tecnoempleo"]
    assert resultado.failed_sources == ["infojobs"]
    assert resultado.ranked_by == "date"


# --------------------------------------------------------------------------- #
# UserProfile
# --------------------------------------------------------------------------- #


def test_perfil_normaliza_y_deduplica_sus_listas():
    perfil = UserProfile(skills=["Python", "python", "PostgreSQL"], exclude_keywords=["Ventas"])
    assert perfil.skills == ["python", "postgresql"]
    assert perfil.exclude_keywords == ["ventas"]


def test_perfil_vacio_se_reconoce_como_vacio():
    assert UserProfile().is_empty
    assert not UserProfile(skills=["python"]).is_empty


def test_perfil_detecta_que_el_usuario_quiere_remoto():
    assert UserProfile(locations=["Madrid", "Remoto"]).wants_remote
    assert not UserProfile(locations=["Madrid"]).wants_remote


def test_perfil_rechaza_claves_desconocidas():
    # Un typo en profile.yaml debe avisar, no ignorarse en silencio.
    with pytest.raises(ValidationError):
        UserProfile(skils=["python"])  # type: ignore[call-arg]
