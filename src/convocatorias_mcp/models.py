"""Modelos de dominio: convocatorias oficiales, watchlists y estado de ingesta.

Este módulo es el único sitio donde se define *qué* es una convocatoria. Las
fuentes producen :class:`Convocatoria`, el índice las guarda, el ranking las
puntúa contra las :class:`Watchlist` del usuario y el servidor MCP las
serializa. Ninguna de esas piezas conoce la estructura de ningún boletín.

Dos reglas que atraviesan todo el módulo:

* **Nada se inventa.** Un campo que no se puede determinar queda en ``None`` y
  se marca como desconocido. Un plazo inventado haría que alguien perdiera una
  convocatoria.
* **Esto no decide elegibilidad.** El score mide afinidad de palabras clave,
  sección y ámbito. No dice, ni puede decir, si el usuario cumple los
  requisitos. Ver :data:`AVISO_NO_ELEGIBILIDAD`.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

__all__ = [
    "AVISO_NO_ELEGIBILIDAD",
    "Ambito",
    "Convocatoria",
    "Estado",
    "EstadoIngesta",
    "MatchComponent",
    "MatchScore",
    "NotificacionConfig",
    "PlazoOrigen",
    "SearchFilters",
    "Seccion",
    "Seguimiento",
    "SourceHealth",
    "Watchlist",
    "WatchlistsConfig",
    "normalize_text",
    "strip_accents",
]

#: Texto que el servidor MCP adjunta a toda respuesta que hable de requisitos.
#: Vive aquí, y no en el prompt de cada tool, para que no se pueda omitir por
#: descuido: equivocarse sobre si alguien puede optar a una ayuda tiene
#: consecuencias reales.
AVISO_NO_ELEGIBILIDAD = (
    "Esta herramienta no determina si cumples los requisitos. Resume lo que pide "
    "la convocatoria y señala coincidencias y discrepancias con tu perfil, pero la "
    "única fuente válida es el texto oficial enlazado."
)


# --------------------------------------------------------------------------- #
# Normalización de texto
# --------------------------------------------------------------------------- #

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z ]+")


def strip_accents(value: str) -> str:
    """Quita tildes y diacríticos: "Ingeniería" -> "Ingenieria"."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_text(value: str) -> str:
    """Forma canónica para comparar texto libre.

    Minúsculas, sin acentos, sin puntuación y con espacios colapsados. Es la
    base del matching de nivel 1, que compara palabras clave contra títulos
    escritos en prosa administrativa.
    """
    folded = _NON_ALNUM_RE.sub(" ", strip_accents(value).lower())
    return _WHITESPACE_RE.sub(" ", folded).strip()


# --------------------------------------------------------------------------- #
# Enumeraciones
# --------------------------------------------------------------------------- #


class Seccion(StrEnum):
    """Sección del boletín, con la etiqueta romana que usa la gente.

    Ojo con esto, que es una trampa verificada contra la API real: el sumario
    del BOE **no** usa ``"II.B"`` ni ``"III"``, sino códigos como ``"2B"`` y
    ``"3"``. El valor del enum es la etiqueta romana, que es la que el usuario
    escribe en su ``watchlists.yaml``, y :meth:`from_api_code` traduce desde el
    código del sumario.
    """

    I = "I"  # noqa: E741 - es la etiqueta oficial de la sección, no una variable
    II_A = "II.A"
    II_B = "II.B"
    III = "III"
    IV = "IV"
    V_A = "V.A"
    V_B = "V.B"
    V_C = "V.C"
    DESCONOCIDA = "desconocida"

    @property
    def codigo_api(self) -> str | None:
        """Código con el que esta sección aparece en el sumario del BOE."""
        return _SECCION_A_API.get(self)

    @property
    def descripcion(self) -> str:
        """Nombre legible, para el dashboard y el digest."""
        return _SECCION_DESCRIPCION.get(self, "Sección desconocida")

    @property
    def es_de_interes(self) -> bool:
        """``True`` en las secciones donde aparecen convocatorias.

        II.B trae oposiciones y concursos, III las bases reguladoras de becas y
        subvenciones, V.A contratación pública y V.B los extractos de
        convocatorias. El resto es normativa, nombramientos o justicia.
        """
        return self in (Seccion.II_B, Seccion.III, Seccion.V_A, Seccion.V_B)

    @property
    def orden(self) -> int:
        """Posición de la sección en el boletín, para ordenar de forma estable.

        No se usa el orden alfabético del valor aunque coincida: que ``"II.B"``
        vaya antes de ``"III"`` en ASCII es una casualidad del punto, y una
        sección nueva podría romperla sin que nadie se diera cuenta. Hay un
        test que comprueba que ambos órdenes siguen coincidiendo, porque la
        consulta SQL ordena por el texto.
        """
        return _SECCION_ORDEN.get(self, 99)

    @classmethod
    def from_api_code(cls, codigo: str | None) -> Seccion:
        """Traduce el código del sumario (``"2B"``) a la sección (``II.B``)."""
        if not codigo:
            return cls.DESCONOCIDA
        return _API_A_SECCION.get(str(codigo).strip().upper(), cls.DESCONOCIDA)

    @classmethod
    def from_text(cls, value: str | None) -> Seccion:
        """Acepta cualquiera de las dos formas, para ser amable con el YAML.

        ``"II.B"``, ``"ii.b"``, ``"IIB"`` y ``"2B"`` son la misma sección.
        """
        if not value:
            return cls.DESCONOCIDA
        crudo = str(value).strip().upper().replace(" ", "")
        if crudo in _API_A_SECCION:
            return _API_A_SECCION[crudo]
        con_puntos = crudo.replace(".", "")
        for seccion in cls:
            if seccion is cls.DESCONOCIDA:
                continue
            if con_puntos == seccion.value.replace(".", ""):
                return seccion
        return cls.DESCONOCIDA


_SECCION_A_API: dict[Seccion, str] = {
    Seccion.I: "1",
    Seccion.II_A: "2A",
    Seccion.II_B: "2B",
    Seccion.III: "3",
    Seccion.IV: "4",
    Seccion.V_A: "5A",
    Seccion.V_B: "5B",
    Seccion.V_C: "5C",
}

_API_A_SECCION: dict[str, Seccion] = {codigo: seccion for seccion, codigo in _SECCION_A_API.items()}

_SECCION_ORDEN: dict[Seccion, int] = {
    Seccion.I: 0,
    Seccion.II_A: 1,
    Seccion.II_B: 2,
    Seccion.III: 3,
    Seccion.IV: 4,
    Seccion.V_A: 5,
    Seccion.V_B: 6,
    Seccion.V_C: 7,
    Seccion.DESCONOCIDA: 99,
}

_SECCION_DESCRIPCION: dict[Seccion, str] = {
    Seccion.I: "I. Disposiciones generales",
    Seccion.II_A: "II.A. Nombramientos, situaciones e incidencias",
    Seccion.II_B: "II.B. Oposiciones y concursos",
    Seccion.III: "III. Otras disposiciones",
    Seccion.IV: "IV. Administración de Justicia",
    Seccion.V_A: "V.A. Anuncios de licitaciones públicas",
    Seccion.V_B: "V.B. Otros anuncios oficiales",
    Seccion.V_C: "V.C. Anuncios particulares",
}


class Estado(StrEnum):
    """Estado de seguimiento de una convocatoria para el usuario."""

    NUEVA = "nueva"
    VISTA = "vista"
    DESCARTADA = "descartada"
    SOLICITADA = "solicitada"


class PlazoOrigen(StrEnum):
    """De dónde sale ``plazo_fin``, para no confundir un dato con una suposición."""

    #: No se ha podido determinar. ``plazo_fin`` es ``None``.
    DESCONOCIDO = "desconocido"
    #: Extraído del texto completo de la convocatoria.
    TEXTO = "texto"
    #: El texto dice explícitamente que no hay plazo de cierre.
    SIN_PLAZO = "sin_plazo"


class EstadoIngesta(StrEnum):
    """Resultado de intentar ingerir un día de boletín."""

    OK = "ok"
    #: El boletín no publicó ese día (domingos y algunos festivos). La API
    #: responde 404, pero esto **no es un error**: es información válida.
    SIN_PUBLICACION = "sin_publicacion"
    ERROR = "error"

    @property
    def es_problema(self) -> bool:
        """Solo ``ERROR`` cuenta como degradación de la fuente."""
        return self is EstadoIngesta.ERROR


class Ambito(StrEnum):
    """Ámbito territorial, cuando se puede determinar."""

    ESTATAL = "Estatal"
    AUTONOMICO = "Autonómico"
    LOCAL = "Local"
    UE = "UE"
    DESCONOCIDO = "desconocido"


# --------------------------------------------------------------------------- #
# Afinidad
# --------------------------------------------------------------------------- #


class MatchComponent(BaseModel):
    """Un sumando del score, listo para ``explain_match``."""

    model_config = ConfigDict(extra="ignore")

    nombre: str = Field(description="Dimensión evaluada: keywords, seccion, ambito.")
    peso: float = Field(description="Peso máximo que aporta esta dimensión.")
    ratio: float = Field(ge=0.0, le=1.0, description="Grado de cumplimiento, de 0 a 1.")
    puntos: float = Field(description="Puntos aportados tras ponderar.")
    detalle: str = Field(default="", description="Explicación en lenguaje natural.")


class MatchScore(BaseModel):
    """Por qué una convocatoria encaja con una watchlist.

    Mide **afinidad temática**, no elegibilidad: que una beca de informática
    puntúe alto no dice nada sobre si el usuario cumple sus requisitos de renta,
    expediente o residencia. Esa lectura es del nivel 2, y la hace el modelo
    sobre el texto oficial.
    """

    model_config = ConfigDict(extra="ignore")

    watchlist: str = Field(description="Watchlist contra la que se puntuó.")
    total: float = Field(ge=0.0, le=1.0, description="Score final, de 0 a 1.")
    componentes: list[MatchComponent] = Field(default_factory=list)
    keywords_encontradas: list[str] = Field(default_factory=list)
    keywords_ausentes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Convocatoria
# --------------------------------------------------------------------------- #

_IDENTIFICADOR_RE = re.compile(r"^[A-Z]{3,5}-[A-Z]-\d{4}-\d+$")


class Convocatoria(BaseModel):
    """Un item de un boletín oficial, normalizado e independiente de la fuente.

    Obligatorios solo los campos que todo boletín publica en su sumario:
    identificador, título, URL y fecha. El resto es opcional porque el sumario
    real no siempre lo trae: hay items sin epígrafe y sin número de expediente,
    y el texto completo solo se descarga bajo demanda.
    """

    model_config = ConfigDict(extra="ignore")

    # --- Identidad -------------------------------------------------------- #
    fuente: str = Field(description="Boletín de origen; 'boe' para el estatal.")
    identificador: str = Field(
        description="Identificador oficial y estable, p. ej. 'BOE-A-2026-21635'."
    )

    # --- Contenido del sumario -------------------------------------------- #
    titulo: str = Field(min_length=1)
    seccion: Seccion = Seccion.DESCONOCIDA
    seccion_nombre: str | None = Field(
        default=None, description="Nombre de la sección tal como lo publica el boletín."
    )
    departamento: str | None = None
    epigrafe: str | None = Field(
        default=None, description="Agrupación dentro del departamento; no siempre existe."
    )
    control: str | None = Field(default=None, description="Número de expediente, si consta.")

    #: Fecha de publicación del boletín.
    #:
    #: Es un ``date`` y no un ``datetime`` a propósito: el BOE publica por días
    #: y no da hora en ningún sitio (el sumario trae ``"20261017"`` y el XML del
    #: documento, ``<fecha_publicacion>20261017</fecha_publicacion>``). Guardar
    #: un ``datetime`` obligaría a inventar una hora, que es justo lo que este
    #: modelo no hace. La hora de nuestra ingesta está en ``ingerido_at``.
    fecha_publicacion: date

    # --- Enlaces ---------------------------------------------------------- #
    url_html: str
    url_xml: str
    url_pdf: str | None = Field(
        default=None, description="URL del PDF; en el sumario viene dentro de un objeto."
    )

    # --- Texto completo (bajo demanda) ------------------------------------ #
    texto_completo: str | None = Field(
        default=None, description="Texto íntegro; se descarga solo cuando se pide el detalle."
    )
    texto_descargado_at: datetime | None = Field(
        default=None, description="Cuándo se descargó el texto, para no repetir la petición."
    )

    # --- Plazo ------------------------------------------------------------ #
    plazo_fin: date | None = Field(default=None, description="Último día para solicitar.")
    plazo_origen: PlazoOrigen = PlazoOrigen.DESCONOCIDO

    # --- Clasificación y seguimiento -------------------------------------- #
    ambito: list[str] = Field(default_factory=list, description="Ámbitos territoriales detectados.")
    matches: list[MatchScore] = Field(
        default_factory=list, description="Watchlists que la han hecho coincidir, con su score."
    )
    estado: Estado = Estado.NUEVA
    ingerido_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # --- Validación ------------------------------------------------------- #

    @field_validator("identificador")
    @classmethod
    def _validar_identificador(cls, value: str) -> str:
        value = value.strip().upper()
        if not _IDENTIFICADOR_RE.match(value):
            raise ValueError(
                f"identificador {value!r} no tiene el formato oficial, p. ej. 'BOE-A-2026-21635'"
            )
        return value

    @field_validator("url_html", "url_xml")
    @classmethod
    def _validar_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("las URLs deben ser absolutas http(s)")
        return value

    @field_validator("url_pdf")
    @classmethod
    def _validar_url_opcional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return cls._validar_url(value)

    @field_validator("titulo", "departamento", "epigrafe", "seccion_nombre")
    @classmethod
    def _colapsar_espacios(cls, value: str | None) -> str | None:
        if value is None:
            return None
        limpio = _WHITESPACE_RE.sub(" ", value).strip()
        return limpio or None

    @field_validator("ambito")
    @classmethod
    def _limpiar_ambito(cls, value: list[str]) -> list[str]:
        unicos: dict[str, str] = {}
        for item in value:
            limpio = _WHITESPACE_RE.sub(" ", str(item)).strip()
            if limpio:
                unicos.setdefault(normalize_text(limpio), limpio)
        return list(unicos.values())

    @model_validator(mode="after")
    def _coherencia_del_plazo(self) -> Self:
        """Un plazo y su origen no pueden contradecirse.

        Es la salvaguarda del "nada se inventa": si hay fecha, tuvo que salir de
        algún sitio, y si no hay, el origen es desconocido o no hay plazo.
        """
        if self.plazo_fin is not None and self.plazo_origen is PlazoOrigen.DESCONOCIDO:
            raise ValueError(
                "plazo_fin tiene fecha pero plazo_origen es 'desconocido': "
                "indica de dónde salió la fecha"
            )
        if self.plazo_fin is None and self.plazo_origen is PlazoOrigen.TEXTO:
            raise ValueError("plazo_origen es 'texto' pero no hay plazo_fin")
        return self

    # --- Derivados -------------------------------------------------------- #

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        """Identificador estable y legible, clave primaria del índice.

        Se compone de fuente e identificador oficial en vez de ser un hash
        opaco: el identificador del BOE ya es único y estable, así que hashearlo
        solo quitaría la posibilidad de leer un id en un log o en una URL y
        saber de qué convocatoria se habla. El prefijo de fuente lo mantiene
        único cuando se añadan boletines autonómicos.
        """
        return f"{self.fuente}:{self.identificador}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def watchlists(self) -> list[str]:
        """Nombres de las watchlists que han hecho coincidir esta convocatoria."""
        return [match.watchlist for match in self.matches]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> float | None:
        """Mejor score entre todas las watchlists, o ``None`` si no hay ninguna."""
        return max((match.total for match in self.matches), default=None)

    @property
    def clave_orden(self) -> tuple[int, int, str]:
        """Clave de orden canónico, estable entre ejecuciones.

        El BOE no publica hora, así que la fecha sola deja empatadas a las 270
        convocatorias de un mismo día y el orden quedaría a merced de lo que
        devolviera la base de datos. El desempate es sección y luego
        identificador oficial, los dos deterministas.

        La fecha va negada para que un ``sorted()`` normal dé directamente el
        orden de presentación: lo más reciente primero y, dentro del día, por
        sección ascendente. Es el mismo orden que aplica la consulta SQL.
        """
        return (-self.fecha_publicacion.toordinal(), self.seccion.orden, self.identificador)

    @property
    def texto_descargado(self) -> bool:
        """``True`` si ya tenemos el texto completo y no hay que volver a pedirlo."""
        return self.texto_completo is not None

    @property
    def plazo_desconocido(self) -> bool:
        """``True`` cuando no se ha podido determinar el plazo."""
        return self.plazo_origen is PlazoOrigen.DESCONOCIDO

    def dias_hasta_plazo(self, hoy: date | None = None) -> int | None:
        """Días que faltan para el cierre, o ``None`` si no se conoce.

        Negativo si ya venció. Devolver ``None`` en vez de un número es
        deliberado: quien lo llame tiene que decidir qué enseñar cuando el plazo
        es desconocido, en lugar de recibir un cero engañoso.
        """
        if self.plazo_fin is None:
            return None
        referencia = hoy or datetime.now(UTC).date()
        return (self.plazo_fin - referencia).days

    def vence_en(self, dias: int, *, hoy: date | None = None) -> bool:
        """``True`` si el plazo cierra dentro de ``dias`` y aún no ha vencido."""
        restantes = self.dias_hasta_plazo(hoy)
        return restantes is not None and 0 <= restantes <= dias

    @property
    def texto_para_matching(self) -> str:
        """Todo el texto disponible, normalizado, para el filtro de nivel 1.

        Incluye el texto completo si ya está descargado, pero no lo exige: el
        filtro barato tiene que funcionar sobre lo que trae el sumario.
        """
        partes = (
            self.titulo,
            self.departamento or "",
            self.epigrafe or "",
            self.seccion_nombre or "",
            self.texto_completo or "",
        )
        return normalize_text(" ".join(partes))


class Seguimiento(BaseModel):
    """Lo que el usuario ha hecho con una convocatoria.

    Separado de :class:`Convocatoria` porque tiene otro ciclo de vida: la
    convocatoria la escribe la ingesta y no cambia, mientras que esto lo escribe
    el usuario. ``solicitada`` vale además como registro de solicitudes.
    """

    model_config = ConfigDict(extra="ignore")

    convocatoria_id: str
    estado: Estado
    motivo: str | None = Field(default=None, description="Por qué se descartó.")
    notas: str | None = Field(default=None, description="Notas libres del usuario.")
    fecha_solicitud: date | None = None
    actualizado_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _coherencia(self) -> Self:
        if self.estado is not Estado.DESCARTADA and self.motivo:
            raise ValueError("solo una convocatoria descartada lleva motivo")
        return self


# --------------------------------------------------------------------------- #
# Watchlists
# --------------------------------------------------------------------------- #


class Watchlist(BaseModel):
    """Un conjunto de criterios con nombre.

    Hay varias y no un perfil único porque los criterios de una beca de
    informática no se parecen a los de una vivienda protegida: mezclarlos daría
    un score sin significado. El digest agrupa por watchlist.
    """

    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(min_length=1, max_length=60)
    keywords: list[str] = Field(default_factory=list)
    secciones: list[Seccion] = Field(
        default_factory=list, description="Secciones a vigilar; vacío = todas."
    )
    ambito: list[str] = Field(
        default_factory=list, description="Ámbitos aceptables; vacío = todos."
    )
    min_score: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0

    @field_validator("nombre")
    @classmethod
    def _limpiar_nombre(cls, value: str) -> str:
        return _WHITESPACE_RE.sub(" ", value).strip()

    @field_validator("keywords", "ambito", mode="before")
    @classmethod
    def _aceptar_cadena_suelta(cls, value: object) -> object:
        # Un YAML con "keywords: informatica" en vez de una lista es un
        # despiste habitual y no merece un error.
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("keywords")
    @classmethod
    def _normalizar_keywords(cls, value: list[str]) -> list[str]:
        limpias: list[str] = []
        for item in value:
            normalizada = normalize_text(str(item))
            if normalizada and normalizada not in limpias:
                limpias.append(normalizada)
        return limpias

    @field_validator("secciones", mode="before")
    @classmethod
    def _aceptar_secciones_en_cualquier_forma(cls, value: object) -> object:
        """Permite ``[III, V.B]``, ``["3", "5B"]`` o una sola suelta."""
        if value is None:
            return []
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, list):
            return items
        return [Seccion.from_text(item) if isinstance(item, str) else item for item in items]

    @field_validator("secciones")
    @classmethod
    def _rechazar_secciones_desconocidas(cls, value: list[Seccion]) -> list[Seccion]:
        if Seccion.DESCONOCIDA in value:
            validas = ", ".join(s.value for s in Seccion if s is not Seccion.DESCONOCIDA)
            raise ValueError(f"sección no reconocida; las válidas son: {validas}")
        return list(dict.fromkeys(value))

    @property
    def vigila_todas_las_secciones(self) -> bool:
        return not self.secciones

    def acepta_seccion(self, seccion: Seccion) -> bool:
        """``True`` si esta watchlist vigila esa sección."""
        return self.vigila_todas_las_secciones or seccion in self.secciones


class NotificacionConfig(BaseModel):
    """Ajustes del digest."""

    model_config = ConfigDict(extra="forbid")

    avisar_plazo_dias: Annotated[int, Field(ge=0, le=365)] = 3


class WatchlistsConfig(BaseModel):
    """Contenido de ``watchlists.yaml``.

    Sin watchlists el servidor funciona igual: no puntúa y ordena por fecha. Lo
    que no se tolera es un fichero con un error, porque el usuario creería que
    está vigilando algo que no vigila.
    """

    model_config = ConfigDict(extra="forbid")

    watchlists: list[Watchlist] = Field(default_factory=list)
    notificacion: NotificacionConfig = Field(default_factory=NotificacionConfig)
    #: Texto libre que acompaña a estas watchlists allá donde se muestren. El
    #: dashboard lo enseña: es como el sitio publicado avisa de que sus
    #: watchlists son de ejemplo y no las de nadie.
    nota: str | None = Field(default=None, max_length=300)

    @field_validator("watchlists")
    @classmethod
    def _nombres_unicos(cls, value: list[Watchlist]) -> list[Watchlist]:
        vistos: set[str] = set()
        for watchlist in value:
            clave = normalize_text(watchlist.nombre)
            if clave in vistos:
                raise ValueError(f"hay dos watchlists llamadas {watchlist.nombre!r}")
            vistos.add(clave)
        return value

    @property
    def is_empty(self) -> bool:
        return not self.watchlists

    def get(self, nombre: str) -> Watchlist | None:
        """Busca una watchlist por nombre, tolerante a mayúsculas y acentos."""
        objetivo = normalize_text(nombre)
        for watchlist in self.watchlists:
            if normalize_text(watchlist.nombre) == objetivo:
                return watchlist
        return None


# --------------------------------------------------------------------------- #
# Búsqueda y salud de las fuentes
# --------------------------------------------------------------------------- #


class SearchFilters(BaseModel):
    """Criterios de ``search_convocatorias``.

    Todos opcionales: una búsqueda sin filtros devuelve lo más reciente, que es
    una pregunta razonable ("¿qué ha salido hoy?").
    """

    model_config = ConfigDict(extra="forbid")

    keywords: str | None = Field(default=None, description="Texto libre a buscar.")
    watchlist: str | None = Field(default=None, description="Restringe a una watchlist.")
    #: Ámbito territorial. **El BOE no publica este campo**: está vacío en las
    #: ~15.000 convocatorias del índice, así que filtrar por él aquí no
    #: devuelve nada. Se mantiene porque el filtro es correcto y servirá en
    #: cuanto una fuente lo publique (los boletines autonómicos, previsible-
    #: mente), pero por eso `search_convocatorias` no lo expone como parámetro:
    #: un filtro que siempre devuelve cero solo hace perder llamadas al modelo.
    ambito: str | None = None
    seccion: Seccion | None = None
    desde: date | None = Field(
        default=None, description="Solo publicadas en o después de esta fecha."
    )
    hasta: date | None = None
    include_vistas: bool = Field(default=False, description="Incluir las ya enviadas en un digest.")
    include_descartadas: bool = False
    solo_con_plazo_abierto: bool = False
    limite: Annotated[int, Field(ge=1, le=200)] = 50

    @field_validator("keywords")
    @classmethod
    def _limpiar_keywords(cls, value: str | None) -> str | None:
        if value is None:
            return None
        limpio = _WHITESPACE_RE.sub(" ", value).strip()
        return limpio or None

    @model_validator(mode="after")
    def _rango_coherente(self) -> Self:
        if self.desde and self.hasta and self.desde > self.hasta:
            raise ValueError("'desde' es posterior a 'hasta'")
        return self

    @property
    def keyword_list(self) -> list[str]:
        """Palabras clave individuales, normalizadas."""
        if not self.keywords:
            return []
        return [palabra for palabra in normalize_text(self.keywords).split(" ") if palabra]


class SourceHealth(BaseModel):
    """Resultado de ingerir un día concreto de una fuente.

    Es la clave de la idempotencia y de la detección de huecos: la ingesta
    consulta esta tabla para saber qué fechas le faltan. Y distingue el día que
    el boletín no publicó (información válida) del día que falló la red
    (problema de verdad), que es la diferencia entre una fuente sana y una
    degradada.
    """

    model_config = ConfigDict(extra="ignore")

    fuente: str
    fecha: date = Field(description="Día del boletín, no de la ingesta.")
    estado: EstadoIngesta
    items: int = Field(default=0, ge=0, description="Convocatorias ingeridas ese día.")
    detalle: str | None = Field(default=None, description="Mensaje de error, si hubo.")
    ingerido_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _coherencia(self) -> Self:
        if self.estado is EstadoIngesta.ERROR and not self.detalle:
            raise ValueError("un estado 'error' tiene que explicar qué falló")
        if self.estado is EstadoIngesta.SIN_PUBLICACION and self.items:
            raise ValueError("un día sin publicación no puede traer items")
        return self

    @property
    def es_problema(self) -> bool:
        """``True`` solo si fue un error real, no un día sin publicación."""
        return self.estado.es_problema
