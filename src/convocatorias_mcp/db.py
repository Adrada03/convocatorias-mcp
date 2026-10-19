"""Índice local en SQLite: esquema, conexión y estado.

La API del BOE devuelve sumarios **por fecha** y no permite buscar, así que la
búsqueda solo es posible sobre un índice propio. Este módulo define ese índice:
el esquema, cómo se abre y cómo se consulta su salud. Las consultas de ingesta y
de búsqueda viven en sus propios módulos; aquí está lo que todos comparten.

Decisiones del esquema:

* **La clave es el identificador oficial** (``BOE-A-2026-21635``), que es
  estable. Eso hace la ingesta idempotente sin esfuerzo: reingerir una fecha es
  un ``INSERT ... ON CONFLICT DO UPDATE`` y no duplica nada.
* **``source_health`` tiene una fila por fuente y día**, y es lo que convierte
  la ingesta en incremental: las fechas que faltan son las que no están aquí.
  Distingue el día que el boletín no publicó del día que falló la red.
* **El seguimiento va en su propia tabla.** La convocatoria la escribe la
  ingesta y no cambia; lo que el usuario hace con ella cambia a menudo.
* **Fechas en TEXT ISO-8601**, que en SQLite ordena y compara correctamente.
* **``titulo_normalizado``** guarda el título sin acentos ni puntuación, porque
  el filtro de nivel 1 compara palabras clave contra prosa administrativa y
  nadie escribe "informática" con tilde en una búsqueda.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

__all__ = [
    "SCHEMA",
    "SCHEMA_VERSION",
    "EstadoIndice",
    "IndexSchemaError",
    "connect",
    "estado_del_indice",
    "initialize",
    "open_index",
    "schema_version",
]

#: Versión del esquema. Se sube cuando cambia de forma incompatible.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS convocatorias (
    id                  TEXT PRIMARY KEY,
    fuente              TEXT NOT NULL,
    identificador       TEXT NOT NULL UNIQUE,
    titulo              TEXT NOT NULL,
    titulo_normalizado  TEXT NOT NULL,
    seccion             TEXT NOT NULL,
    seccion_nombre      TEXT,
    departamento        TEXT,
    epigrafe            TEXT,
    control             TEXT,
    fecha_publicacion   TEXT NOT NULL,
    url_html            TEXT NOT NULL,
    url_xml             TEXT NOT NULL,
    url_pdf             TEXT,
    texto_completo      TEXT,
    texto_descargado_at TEXT,
    plazo_fin           TEXT,
    plazo_origen        TEXT NOT NULL DEFAULT 'desconocido',
    ambito              TEXT NOT NULL DEFAULT '[]',
    ingerido_at         TEXT NOT NULL,
    actualizado_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_convocatorias_fecha
    ON convocatorias (fecha_publicacion DESC);
CREATE INDEX IF NOT EXISTS idx_convocatorias_seccion
    ON convocatorias (seccion, fecha_publicacion DESC);
CREATE INDEX IF NOT EXISTS idx_convocatorias_plazo
    ON convocatorias (plazo_fin) WHERE plazo_fin IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_convocatorias_fuente
    ON convocatorias (fuente, fecha_publicacion DESC);

-- Una fila por convocatoria y watchlist coincidente. Normalizado en vez de un
-- JSON en la convocatoria para poder filtrar por watchlist en SQL.
CREATE TABLE IF NOT EXISTS convocatoria_watchlists (
    convocatoria_id TEXT NOT NULL
        REFERENCES convocatorias (id) ON DELETE CASCADE,
    watchlist       TEXT NOT NULL,
    score           REAL NOT NULL,
    detalle         TEXT,
    PRIMARY KEY (convocatoria_id, watchlist)
);

CREATE INDEX IF NOT EXISTS idx_watchlists_score
    ON convocatoria_watchlists (watchlist, score DESC);

CREATE TABLE IF NOT EXISTS seguimiento (
    convocatoria_id TEXT PRIMARY KEY
        REFERENCES convocatorias (id) ON DELETE CASCADE,
    estado          TEXT NOT NULL,
    motivo          TEXT,
    notas           TEXT,
    fecha_solicitud TEXT,
    actualizado_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_seguimiento_estado ON seguimiento (estado);

-- Salud de la ingesta, por fuente y dia del boletin. Es el registro que hace
-- la ingesta incremental e idempotente: lo que no esta aqui, falta.
CREATE TABLE IF NOT EXISTS source_health (
    fuente      TEXT NOT NULL,
    fecha       TEXT NOT NULL,
    estado      TEXT NOT NULL,
    items       INTEGER NOT NULL DEFAULT 0,
    detalle     TEXT,
    ingerido_at TEXT NOT NULL,
    PRIMARY KEY (fuente, fecha)
);

CREATE INDEX IF NOT EXISTS idx_source_health_estado
    ON source_health (fuente, estado);

-- Registro de digests enviados. No guarda el destinatario a proposito: es un
-- dato personal que no hace falta para saber si un digest ya salio.
CREATE TABLE IF NOT EXISTS digest_envios (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    enviado_at    TEXT NOT NULL,
    convocatorias INTEGER NOT NULL DEFAULT 0,
    avisos_plazo  INTEGER NOT NULL DEFAULT 0,
    dry_run       INTEGER NOT NULL DEFAULT 0
);
"""


class IndexSchemaError(Exception):
    """El índice existe pero su esquema no es el que este código entiende."""


def connect(path: Path | str) -> sqlite3.Connection:
    """Abre la base de datos y aplica los ajustes que queremos siempre.

    ``WAL`` permite que el export del dashboard lea mientras la ingesta
    escribe, y ``foreign_keys`` hace que los ``ON DELETE CASCADE`` del esquema
    funcionen de verdad: en SQLite están desactivados por defecto.
    """
    ruta = Path(path)
    if ruta.parent and str(ruta.parent) not in ("", "."):
        ruta.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(ruta, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int | None:
    """Versión del esquema del índice, o ``None`` si está vacío."""
    tabla = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if tabla is None:
        return None
    fila = conn.execute("SELECT valor FROM meta WHERE clave = 'schema_version'").fetchone()
    return int(fila["valor"]) if fila else None


def initialize(conn: sqlite3.Connection) -> None:
    """Crea el esquema si falta y comprueba la versión si ya existe."""
    actual = schema_version(conn)
    if actual is not None and actual != SCHEMA_VERSION:
        raise IndexSchemaError(
            f"el índice usa el esquema v{actual} y este código espera v{SCHEMA_VERSION}; "
            "borra el fichero del índice y vuelve a ingerir"
        )

    # Sin transaccion explicita a proposito: executescript emite un COMMIT
    # implicito antes de ejecutar, asi que no puede ir dentro de un BEGIN. El
    # DDL es idempotente (IF NOT EXISTS) y la conexion va en autocommit.
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta (clave, valor) VALUES ('schema_version', ?) "
        "ON CONFLICT (clave) DO UPDATE SET valor = excluded.valor",
        (str(SCHEMA_VERSION),),
    )


def open_index(path: Path | str) -> sqlite3.Connection:
    """Abre el índice listo para usar: conexión configurada y esquema aplicado."""
    conn = connect(path)
    try:
        initialize(conn)
    except Exception:
        conn.close()
        raise
    return conn


# --------------------------------------------------------------------------- #
# Estado del índice
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EstadoIndice:
    """Resumen del índice, para ``doctor`` y para el tool ``source_health``."""

    convocatorias: int = 0
    dias_ok: int = 0
    dias_sin_publicacion: int = 0
    dias_con_error: int = 0
    #: Hasta cuándo se ha *comprobado* la fuente, de ``source_health``.
    primera_fecha: date | None = None
    ultima_fecha: date | None = None
    #: Fecha de publicación más reciente de la que hay *datos*. No es lo mismo:
    #: el último día comprobado pudo ser un domingo sin publicación.
    ultima_publicacion: date | None = None
    huecos: list[date] = field(default_factory=list)

    @property
    def dias_registrados(self) -> int:
        """Días que se han intentado ingerir, con el resultado que fuera."""
        return self.dias_ok + self.dias_sin_publicacion + self.dias_con_error

    @property
    def esta_vacio(self) -> bool:
        return self.dias_registrados == 0

    def resumen(self) -> str:
        """Una línea legible para la salida de ``doctor``."""
        if self.esta_vacio:
            return "vacío; ejecuta 'convocatorias-mcp ingest' para poblarlo"
        partes = [
            f"{self.convocatorias} convocatorias",
            f"{self.dias_registrados} días registrados",
            f"último {self.ultima_fecha.isoformat()}" if self.ultima_fecha else "sin fechas",
        ]
        if self.dias_sin_publicacion:
            partes.append(f"{self.dias_sin_publicacion} sin publicación")
        if self.dias_con_error:
            partes.append(f"{self.dias_con_error} con error")
        if self.huecos:
            partes.append(f"{len(self.huecos)} huecos")
        return ", ".join(partes)


def estado_del_indice(conn: sqlite3.Connection, *, fuente: str | None = None) -> EstadoIndice:
    """Calcula el estado del índice a partir de ``source_health``.

    Un "hueco" es una fecha dentro del rango ya cubierto que no se ha intentado
    ingerir nunca. Los domingos no se excluyen: la ingesta pide todas las fechas
    y registra el 404 como "sin publicación", así que un domingo ingerido no es
    un hueco, y un domingo que falta sí lo es.
    """
    filtro = "WHERE fuente = ?" if fuente else ""
    params: tuple[str, ...] = (fuente,) if fuente else ()

    fila = conn.execute(
        f"""
        SELECT
            COUNT(*)                                                   AS dias,
            SUM(estado = 'ok')                                         AS dias_ok,
            SUM(estado = 'sin_publicacion')                            AS sin_publicacion,
            SUM(estado = 'error')                                      AS con_error,
            MIN(fecha)                                                 AS primera,
            MAX(fecha)                                                 AS ultima
        FROM source_health
        {filtro}
        """,
        params,
    ).fetchone()

    resumen_datos = conn.execute(
        f"SELECT COUNT(*) AS total, MAX(fecha_publicacion) AS ultima FROM convocatorias {filtro}",
        params,
    ).fetchone()
    convocatorias = resumen_datos["total"]
    ultima_publicacion = (
        date.fromisoformat(resumen_datos["ultima"]) if resumen_datos["ultima"] else None
    )

    if not fila or not fila["dias"]:
        return EstadoIndice(convocatorias=convocatorias, ultima_publicacion=ultima_publicacion)

    primera = date.fromisoformat(fila["primera"])
    ultima = date.fromisoformat(fila["ultima"])

    registradas = {
        date.fromisoformat(row["fecha"])
        for row in conn.execute(f"SELECT fecha FROM source_health {filtro}", params)
    }
    huecos = []
    dia = primera
    while dia <= ultima:
        if dia not in registradas:
            huecos.append(dia)
        dia += timedelta(days=1)

    return EstadoIndice(
        convocatorias=convocatorias,
        dias_ok=fila["dias_ok"] or 0,
        dias_sin_publicacion=fila["sin_publicacion"] or 0,
        dias_con_error=fila["con_error"] or 0,
        primera_fecha=primera,
        ultima_fecha=ultima,
        ultima_publicacion=ultima_publicacion,
        huecos=huecos,
    )
