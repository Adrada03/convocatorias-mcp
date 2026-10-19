"""Fixtures compartidas por toda la suite.

Regla de la suite: ningún test toca la red. Las fuentes HTTP se prueban con
``respx``, y los parsers con HTML real guardado en ``tests/fixtures/``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from jobscout_mcp.models import ContractType, JobListing, Seniority, WorkMode
from jobscout_mcp.sources.base import RateLimiter, RobotsPolicy


@pytest.fixture(autouse=True)
def _clean_shared_state() -> None:
    """El rate limiter y la caché de robots.txt son estado de proceso.

    Se limpian antes de cada test para que el orden de ejecución no influya.
    """
    RateLimiter.reset()
    RobotsPolicy.reset_cache()


@pytest.fixture
def listing_factory():
    """Crea ofertas con valores por defecto razonables.

    Cada test sobrescribe solo los campos que le importan, lo que mantiene los
    tests legibles y resistentes a que el modelo gane campos.
    """

    def _make(**overrides: object) -> JobListing:
        data: dict[str, object] = {
            "source": "tecnoempleo",
            "source_url": "https://www.tecnoempleo.com/oferta/rf-1",
            "external_id": "rf-1",
            "title": "Backend Python Developer",
            "company": "Acme S.L.",
            "location": "Madrid",
            "work_mode": WorkMode.REMOTE,
            "contract_type": ContractType.FULL_TIME,
            "seniority": Seniority.JUNIOR,
            "posted_at": date(2026, 10, 15),
            "excerpt": "Buscamos backend con Python y PostgreSQL.",
            "tags": ["Python", "FastAPI", "PostgreSQL"],
            "fetched_at": datetime(2026, 10, 19, 10, 0, tzinfo=UTC),
        }
        data.update(overrides)
        return JobListing(**data)  # type: ignore[arg-type]

    return _make
