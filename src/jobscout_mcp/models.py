"""Modelos de dominio compartidos por todo el proyecto.

Este módulo es deliberadamente el único sitio donde se define *qué* es una
oferta de empleo. Los adaptadores de portales producen :class:`JobListing`,
el aggregator los deduplica, ``ranking`` los puntúa y el servidor MCP los
serializa. Ninguna de esas piezas conoce la estructura de ningún portal.

Nota de diseño: ``source_url`` es un ``str`` validado en lugar de
``pydantic.HttpUrl``. Los listings viajan a SQLite y a JSON (caché, dashboard,
respuestas MCP) y se comparan en los tests; un ``str`` hace ese ida y vuelta
trivial sin perder la validación de esquema.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

__all__ = [
    "ContractType",
    "JobListing",
    "MatchComponent",
    "MatchScore",
    "SalaryPeriod",
    "SalaryRange",
    "SavedSearch",
    "SearchFilters",
    "SearchResult",
    "Seniority",
    "SourceStatus",
    "UserProfile",
    "WorkMode",
    "dedupe_key",
    "normalize_company",
    "normalize_text",
    "normalize_title",
    "strip_accents",
]


# --------------------------------------------------------------------------- #
# Normalización de texto
# --------------------------------------------------------------------------- #

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z ]+")

# Sufijos societarios: "ACME S.L." y "Acme SL" son la misma empresa.
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(s\s?l\s?u|s\s?l|s\s?a\s?u|s\s?a|s\s?coop|sociedad limitada|"
    r"inc|ltd|llc|gmbh|bv|plc|group|grupo|spain|espana)\b"
)

# Ruido habitual en los títulos de oferta que no distingue un puesto de otro.
_TITLE_NOISE_RE = re.compile(
    r"\b(h\s?m\s?x?|m\s?f\s?d?|m\s?w\s?d|teletrabajo|100 remoto|remoto|remote|"
    r"hibrido|hybrid|presencial|nueva|urgente|oferta)\b"
)


def strip_accents(value: str) -> str:
    """Quita tildes y diacríticos: "Diseñador Sénior" -> "Disenador Senior"."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_text(value: str) -> str:
    """Forma canónica para comparar texto libre.

    Minúsculas, sin acentos, sin puntuación y con espacios colapsados. Es la
    base de la deduplicación y del matching de skills, así que vive aquí y no
    duplicada en cada módulo.
    """
    folded = strip_accents(value).lower()
    folded = _NON_ALNUM_RE.sub(" ", folded)
    return _WHITESPACE_RE.sub(" ", folded).strip()


def normalize_company(value: str) -> str:
    """Como :func:`normalize_text`, pero además sin sufijos societarios."""
    normalized = _COMPANY_SUFFIX_RE.sub(" ", normalize_text(value))
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def normalize_title(value: str) -> str:
    """Como :func:`normalize_text`, pero además sin ruido típico de títulos."""
    normalized = _TITLE_NOISE_RE.sub(" ", normalize_text(value))
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def dedupe_key(title: str, company: str) -> str:
    """Clave de deduplicación entre portales.

    La misma oferta publicada en dos sitios comparte título y empresa pero no
    URL ni id interno, así que la clave es un hash de ambos normalizados. Se
    devuelve el hash (y no el texto) para que tenga longitud fija y sirva como
    columna indexada en SQLite.
    """
    payload = f"{normalize_title(title)}|{normalize_company(company)}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Enumeraciones
# --------------------------------------------------------------------------- #


class ContractType(StrEnum):
    """Tipo de contrato. ``UNKNOWN`` cuando el portal no lo publica."""

    INTERNSHIP = "internship"
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    FREELANCE = "freelance"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"

    @classmethod
    def from_text(cls, value: str | None) -> ContractType:
        """Mapea el texto libre de un portal español al enum.

        Vive en los modelos, y no en cada adaptador, porque el vocabulario es
        el mismo en todos los portales y así se testea una sola vez.
        """
        if not value:
            return cls.UNKNOWN
        text = normalize_text(value)
        if any(word in text for word in ("beca", "practica", "internship", "trainee")):
            return cls.INTERNSHIP
        if any(word in text for word in ("freelance", "autonomo", "mercantil")):
            return cls.FREELANCE
        if any(word in text for word in ("parcial", "part time")):
            return cls.PART_TIME
        if any(word in text for word in ("temporal", "obra y servicio", "sustitucion")):
            return cls.TEMPORARY
        if any(word in text for word in ("indefinido", "completa", "full time", "fijo")):
            return cls.FULL_TIME
        return cls.UNKNOWN


class WorkMode(StrEnum):
    """Modalidad de trabajo."""

    ON_SITE = "on_site"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"

    @classmethod
    def from_text(cls, value: str | None) -> WorkMode:
        """Mapea cadenas como "100% remoto" o "Híbrido 2 días"."""
        if not value:
            return cls.UNKNOWN
        text = normalize_text(value)
        if any(word in text for word in ("hibrido", "hybrid", "mixto")):
            return cls.HYBRID
        if any(word in text for word in ("remoto", "remote", "teletrabajo", "en casa")):
            return cls.REMOTE
        if any(word in text for word in ("presencial", "on site", "oficina")):
            return cls.ON_SITE
        return cls.UNKNOWN

    @property
    def is_remote_friendly(self) -> bool:
        """``True`` para remoto e híbrido: lo que satisface un filtro ``remote=True``."""
        return self in (WorkMode.REMOTE, WorkMode.HYBRID)


class Seniority(StrEnum):
    """Nivel de experiencia que pide la oferta."""

    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    UNKNOWN = "unknown"

    @classmethod
    def from_text(cls, value: str | None) -> Seniority:
        """Deduce el nivel a partir del título o del cuerpo de la oferta."""
        if not value:
            return cls.UNKNOWN
        text = normalize_text(value)
        if any(word in text for word in ("becario", "beca", "practicas", "intern", "trainee")):
            return cls.INTERN
        if any(word in text for word in ("lead", "principal", "staff", "arquitecto")):
            return cls.LEAD
        if any(word in text for word in ("senior", "sr", "experto")):
            return cls.SENIOR
        if any(word in text for word in ("junior", "jr", "iniciacion")):
            return cls.JUNIOR
        if any(word in text for word in ("semi senior", "mid", "intermedio")):
            return cls.MID
        return cls.UNKNOWN

    @property
    def rank(self) -> int:
        """Orden numérico para medir distancia entre niveles (``UNKNOWN`` -> ``-1``)."""
        order = {
            Seniority.INTERN: 0,
            Seniority.JUNIOR: 1,
            Seniority.MID: 2,
            Seniority.SENIOR: 3,
            Seniority.LEAD: 4,
        }
        return order.get(self, -1)


class SalaryPeriod(StrEnum):
    """Periodicidad del salario publicado."""

    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    HOUR = "hour"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Oferta de empleo
# --------------------------------------------------------------------------- #


class SalaryRange(BaseModel):
    """Salario publicado, conservando siempre el texto original.

    Los portales escriben el salario de formas muy distintas
    ("33.000€ - 39.000€ b/a", "Según convenio"), así que ``raw`` es la fuente
    de verdad y los campos numéricos son un extra best-effort.
    """

    model_config = ConfigDict(extra="ignore")

    raw: str = Field(description="Texto tal cual lo publica el portal.")
    min_amount: float | None = Field(default=None, ge=0)
    max_amount: float | None = Field(default=None, ge=0)
    currency: str = Field(default="EUR", max_length=3)
    period: SalaryPeriod = SalaryPeriod.UNKNOWN

    @model_validator(mode="after")
    def _order_bounds(self) -> Self:
        if (
            self.min_amount is not None
            and self.max_amount is not None
            and self.min_amount > self.max_amount
        ):
            self.min_amount, self.max_amount = self.max_amount, self.min_amount
        return self


class MatchComponent(BaseModel):
    """Un sumando del score de afinidad, listo para ``explain_match``."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="Dimensión evaluada: skills, location, seniority...")
    weight: float = Field(description="Puntos máximos que puede aportar esta dimensión.")
    ratio: float = Field(ge=-1.0, le=1.0, description="Grado de cumplimiento, de -1 a 1.")
    points: float = Field(description="Puntos aportados (negativos si es penalización).")
    detail: str = Field(default="", description="Explicación en lenguaje natural.")


class MatchScore(BaseModel):
    """Desglose completo del score de una oferta frente a un perfil."""

    model_config = ConfigDict(extra="ignore")

    total: float = Field(ge=0.0, le=100.0, description="Score final normalizado a 0-100.")
    components: list[MatchComponent] = Field(default_factory=list)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    matched_nice_to_have: list[str] = Field(default_factory=list)
    triggered_exclusions: list[str] = Field(default_factory=list)


class JobListing(BaseModel):
    """Una oferta de empleo normalizada, independiente del portal de origen.

    Solo ``source``, ``source_url``, ``title`` y ``company`` son obligatorios:
    es el mínimo que todo portal publica en su página de resultados. Lo demás
    es opcional porque no todos lo dan, y un adaptador que no encuentra un
    campo debe dejarlo en ``None`` en vez de inventarlo.
    """

    model_config = ConfigDict(extra="ignore")

    # --- Identidad -------------------------------------------------------- #
    source: str = Field(description="Nombre del adaptador que produjo la oferta.")
    source_url: str = Field(description="URL canónica de la oferta en el portal.")
    external_id: str | None = Field(
        default=None, description="Id interno del portal, si lo expone (p. ej. rf-12345)."
    )

    # --- Contenido -------------------------------------------------------- #
    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    location: str | None = None
    work_mode: WorkMode = WorkMode.UNKNOWN
    contract_type: ContractType = ContractType.UNKNOWN
    seniority: Seniority = Seniority.UNKNOWN
    salary: SalaryRange | None = None
    posted_at: date | None = Field(default=None, description="Fecha de publicación, si consta.")
    excerpt: str | None = Field(default=None, description="Resumen de la página de resultados.")
    description: str | None = Field(
        default=None, description="Descripción completa, si se pidió el detalle."
    )
    tags: list[str] = Field(
        default_factory=list, description="Tecnologías declaradas por el portal."
    )

    # --- Metadatos del agregador ------------------------------------------ #
    seen_in_sources: list[str] = Field(
        default_factory=list,
        description="Portales donde se encontró esta oferta (más de uno si hubo duplicado).",
    )
    score: float | None = Field(default=None, description="Score de afinidad; None sin perfil.")
    match: MatchScore | None = Field(default=None, description="Desglose del score.")
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("source_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("source_url debe ser una URL http(s) absoluta")
        return value

    @field_validator("title", "company")
    @classmethod
    def _collapse_whitespace(cls, value: str) -> str:
        return _WHITESPACE_RE.sub(" ", value).strip()

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        unique: dict[str, str] = {}
        for tag in value:
            cleaned = _WHITESPACE_RE.sub(" ", tag).strip()
            if cleaned:
                unique.setdefault(normalize_text(cleaned), cleaned)
        return list(unique.values())

    @model_validator(mode="after")
    def _ensure_provenance(self) -> Self:
        if not self.seen_in_sources:
            self.seen_in_sources = [self.source]
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        """Id estable entre ejecuciones, usado por ``get_job_details`` y la caché.

        Se deriva del portal y de su id interno (o de la URL si no hay id), de
        forma que la misma oferta recupere siempre el mismo identificador sin
        secuencias en base de datos.
        """
        suffix = self.external_id or hashlib.sha1(self.source_url.encode("utf-8")).hexdigest()[:12]
        return f"{self.source}:{suffix}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dedupe_key(self) -> str:
        """Clave de agrupación de duplicados entre portales."""
        return dedupe_key(self.title, self.company)

    @property
    def completeness(self) -> int:
        """Cuántos campos informativos trae, para elegir el mejor de dos duplicados."""
        optional = (
            self.location,
            self.salary,
            self.posted_at,
            self.excerpt,
            self.description,
            self.external_id,
        )
        filled = sum(1 for value in optional if value)
        if self.work_mode is not WorkMode.UNKNOWN:
            filled += 1
        if self.contract_type is not ContractType.UNKNOWN:
            filled += 1
        if self.seniority is not Seniority.UNKNOWN:
            filled += 1
        return filled + len(self.tags)

    @property
    def searchable_text(self) -> str:
        """Todo el texto de la oferta junto y normalizado, para el matching de skills."""
        parts = (
            self.title,
            self.company,
            self.location or "",
            self.excerpt or "",
            self.description or "",
            " ".join(self.tags),
        )
        return normalize_text(" ".join(parts))


# --------------------------------------------------------------------------- #
# Búsqueda
# --------------------------------------------------------------------------- #


class SearchFilters(BaseModel):
    """Criterios de búsqueda, idénticos para todos los portales.

    Cada adaptador traduce a su propia query lo que su portal sepa filtrar y
    deja el resto; el aggregator aplica :meth:`matches` sobre los resultados
    para que el filtrado final sea consistente aunque un portal ignore un
    criterio.
    """

    model_config = ConfigDict(extra="forbid")

    keywords: str = Field(min_length=2, description="Texto libre: backend python, devops...")
    location: str | None = Field(default=None, description="Provincia o ciudad.")
    remote: bool | None = Field(default=None, description="True para solo remoto o híbrido.")
    contract_type: ContractType | None = None
    posted_within_days: Annotated[int, Field(ge=1, le=365)] | None = None
    sources: list[str] | None = Field(
        default=None, description="Portales a consultar; None = todos los activos."
    )
    limit_per_source: Annotated[int, Field(ge=1, le=100)] = 20

    @field_validator("keywords")
    @classmethod
    def _trim_keywords(cls, value: str) -> str:
        return _WHITESPACE_RE.sub(" ", value).strip()

    @property
    def keyword_list(self) -> list[str]:
        """Palabras clave individuales, normalizadas."""
        return [word for word in normalize_text(self.keywords).split(" ") if word]

    def matches(self, listing: JobListing, *, today: date | None = None) -> bool:
        """Comprueba los filtros que el portal pudo haber ignorado.

        Es permisivo con lo desconocido: una oferta sin tipo de contrato o sin
        fecha no se descarta, porque descartarla castigaría al portal que
        publica menos metadatos en lugar de a la oferta.
        """
        if self.remote and not listing.work_mode.is_remote_friendly:
            return False
        if (
            self.contract_type
            and listing.contract_type is not ContractType.UNKNOWN
            and listing.contract_type is not self.contract_type
        ):
            return False
        if self.posted_within_days is not None and listing.posted_at is not None:
            reference = today or datetime.now(UTC).date()
            if (reference - listing.posted_at).days > self.posted_within_days:
                return False
        return True


class SourceStatus:
    """Valores de ``SearchResult.source_status``.

    Es un espacio de nombres con constantes, y no un enum, porque el estado de
    error lleva el mensaje incrustado ("error: timeout"), que es justo lo que
    el usuario necesita leer en la respuesta del tool MCP.
    """

    OK = "ok"
    SKIPPED = "skipped"
    _ERROR_PREFIX = "error: "

    @classmethod
    def error(cls, message: object) -> str:
        """Construye un estado de error legible a partir de un mensaje o excepción."""
        return f"{cls._ERROR_PREFIX}{_WHITESPACE_RE.sub(' ', str(message)).strip()}"

    @classmethod
    def is_error(cls, status: str) -> bool:
        return status.startswith(cls._ERROR_PREFIX)


class SearchResult(BaseModel):
    """Resultado agregado de una búsqueda.

    ``source_status`` es la pieza de resiliencia: si un portal se cae, su
    estado lo cuenta y el resto de resultados llega igual. Una búsqueda nunca
    falla entera por culpa de una sola fuente.
    """

    model_config = ConfigDict(extra="ignore")

    filters: SearchFilters
    listings: list[JobListing] = Field(default_factory=list)
    source_status: dict[str, str] = Field(
        default_factory=dict,
        description="Por portal: ok, skipped o error: <mensaje>.",
    )
    total_before_dedupe: int = 0
    duplicates_removed: int = 0
    ranked_by: Literal["score", "date"] = "date"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok_sources(self) -> list[str]:
        return sorted(
            name for name, status in self.source_status.items() if status == SourceStatus.OK
        )

    @property
    def failed_sources(self) -> list[str]:
        return sorted(
            name for name, status in self.source_status.items() if SourceStatus.is_error(status)
        )


class SavedSearch(BaseModel):
    """Búsqueda guardada por el usuario, persistida en SQLite."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=80)
    filters: SearchFilters
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_run_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        return _WHITESPACE_RE.sub(" ", value).strip()


# --------------------------------------------------------------------------- #
# Perfil de usuario
# --------------------------------------------------------------------------- #


class UserProfile(BaseModel):
    """Perfil opcional del usuario, cargado de ``profile.yaml``.

    Nada aquí lleva valores por defecto con datos reales: el repo versiona
    solo ``profile.example.yaml`` y cada usuario escribe el suyo. Sin perfil,
    ``ranking`` no puntúa y el aggregator ordena por fecha.
    """

    model_config = ConfigDict(extra="forbid")

    skills: list[str] = Field(default_factory=list, description="Tecnologías que domina.")
    nice_to_have: list[str] = Field(default_factory=list, description="Tecnologías de interés.")
    seniority: Seniority | None = None
    locations: list[str] = Field(default_factory=list, description="Ubicaciones aceptables.")
    contract_types: list[ContractType] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list, description="Descartes duros.")

    @field_validator("skills", "nice_to_have", "exclude_keywords", "locations")
    @classmethod
    def _normalize_list(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            normalized = normalize_text(str(item))
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned

    @property
    def is_empty(self) -> bool:
        """``True`` si el perfil no aporta nada con lo que rankear."""
        return not any(
            (
                self.skills,
                self.nice_to_have,
                self.seniority,
                self.locations,
                self.contract_types,
                self.exclude_keywords,
            )
        )

    @property
    def wants_remote(self) -> bool:
        """``True`` si el usuario incluyó remoto entre sus ubicaciones."""
        return any(loc in ("remoto", "remote", "teletrabajo") for loc in self.locations)
