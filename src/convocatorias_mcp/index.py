"""Lectura y escritura del índice local.

Separado de :mod:`convocatorias_mcp.db`, que define el esquema y la conexión:
aquí están las consultas que usan la ingesta y el servidor MCP.

Dos invariantes que este módulo mantiene:

* **Reingerir no destruye nada.** Un ``upsert`` por identificador oficial nunca
  pisa un ``texto_completo`` ya descargado con un ``NULL``, porque el sumario no
  trae textos y volver a ingerir el día los borraría.
* **El orden es determinista.** El BOE no publica hora, así que 270
  convocatorias de un mismo día empatan en fecha. Sin desempate explícito el
  orden quedaría a merced de lo que devolviera SQLite, y dos consultas
  idénticas podrían dar listas distintas.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime

from convocatorias_mcp.models import (
    Convocatoria,
    Estado,
    EstadoIngesta,
    PlazoOrigen,
    SearchFilters,
    Seccion,
    SourceHealth,
    normalize_text,
)
from convocatorias_mcp.vocabulario import expandir_keywords

__all__ = [
    "ORDEN_CANONICO_SQL",
    "buscar",
    "fechas_registradas",
    "guardar_convocatorias",
    "guardar_texto",
    "obtener_convocatoria",
    "registrar_ingesta",
    "ultima_ingesta",
]

#: Orden de presentación, y el mismo que :attr:`Convocatoria.clave_orden`.
#:
#: Lo más reciente primero y, dentro del día, sección y luego identificador.
#: Se ordena por el texto de la sección porque su orden alfabético coincide con
#: el orden del boletín; hay un test que lo comprueba, para que una sección
#: nueva no rompa el criterio en silencio.
ORDEN_CANONICO_SQL = "c.fecha_publicacion DESC, c.seccion ASC, c.identificador ASC"

_CAMPOS = """
    c.id, c.fuente, c.identificador, c.titulo, c.seccion, c.seccion_nombre,
    c.departamento, c.epigrafe, c.control, c.fecha_publicacion, c.url_html,
    c.url_xml, c.url_pdf, c.texto_completo, c.texto_descargado_at, c.plazo_fin,
    c.plazo_origen, c.ambito, c.ingerido_at,
    COALESCE(s.estado, 'nueva') AS estado
"""


# --------------------------------------------------------------------------- #
# Escritura
# --------------------------------------------------------------------------- #


def guardar_convocatorias(conn: sqlite3.Connection, convocatorias: Iterable[Convocatoria]) -> int:
    """Guarda o actualiza convocatorias. Devuelve cuántas se procesaron.

    Es idempotente: la clave es el identificador oficial, que es estable, así
    que reingerir una fecha actualiza las filas en vez de duplicarlas.
    """
    filas = list(convocatorias)
    if not filas:
        return 0

    ahora = datetime.now().astimezone().isoformat()
    conn.executemany(
        """
        INSERT INTO convocatorias (
            id, fuente, identificador, titulo, titulo_normalizado, seccion,
            seccion_nombre, departamento, epigrafe, control, fecha_publicacion,
            url_html, url_xml, url_pdf, texto_completo, texto_descargado_at,
            plazo_fin, plazo_origen, ambito, ingerido_at, actualizado_at
        ) VALUES (
            :id, :fuente, :identificador, :titulo, :titulo_normalizado, :seccion,
            :seccion_nombre, :departamento, :epigrafe, :control, :fecha_publicacion,
            :url_html, :url_xml, :url_pdf, :texto_completo, :texto_descargado_at,
            :plazo_fin, :plazo_origen, :ambito, :ingerido_at, :actualizado_at
        )
        ON CONFLICT (id) DO UPDATE SET
            titulo              = excluded.titulo,
            titulo_normalizado  = excluded.titulo_normalizado,
            seccion             = excluded.seccion,
            seccion_nombre      = excluded.seccion_nombre,
            departamento        = excluded.departamento,
            epigrafe            = excluded.epigrafe,
            control             = excluded.control,
            fecha_publicacion   = excluded.fecha_publicacion,
            url_html            = excluded.url_html,
            url_xml             = excluded.url_xml,
            url_pdf             = excluded.url_pdf,
            -- El sumario no trae textos: si el nuevo valor es NULL se conserva
            -- el que ya hubiera, para no borrar una descarga previa.
            texto_completo      = COALESCE(excluded.texto_completo, texto_completo),
            texto_descargado_at = COALESCE(
                excluded.texto_descargado_at, texto_descargado_at
            ),
            plazo_fin           = COALESCE(excluded.plazo_fin, plazo_fin),
            plazo_origen        = CASE
                WHEN excluded.plazo_origen = 'desconocido' THEN plazo_origen
                ELSE excluded.plazo_origen
            END,
            ambito              = excluded.ambito,
            actualizado_at      = excluded.actualizado_at
        """,
        [_a_fila(convocatoria, ahora) for convocatoria in filas],
    )
    return len(filas)


def guardar_texto(conn: sqlite3.Connection, convocatoria: Convocatoria) -> bool:
    """Cachea el texto completo de una convocatoria ya indexada.

    Devuelve ``True`` si se guardó. El texto de un BOE publicado no cambia, así
    que esto se escribe una vez y ahorra la petición en todas las consultas
    siguientes: es la caché que exige el trato educado con la fuente.
    """
    if not convocatoria.texto_descargado:
        return False

    descargado = convocatoria.texto_descargado_at or datetime.now(UTC)
    cursor = conn.execute(
        """
        UPDATE convocatorias
        SET texto_completo      = :texto,
            texto_descargado_at = :descargado,
            actualizado_at      = :ahora
        WHERE id = :id
        """,
        {
            "id": convocatoria.id,
            "texto": convocatoria.texto_completo,
            "descargado": descargado.isoformat(),
            "ahora": datetime.now().astimezone().isoformat(),
        },
    )
    return cursor.rowcount > 0


def registrar_ingesta(conn: sqlite3.Connection, salud: SourceHealth) -> None:
    """Registra el resultado de intentar ingerir un día.

    Es lo que hace la ingesta incremental: la próxima vez, las fechas que ya
    están aquí no se vuelven a pedir.
    """
    conn.execute(
        """
        INSERT INTO source_health (fuente, fecha, estado, items, detalle, ingerido_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (fuente, fecha) DO UPDATE SET
            estado      = excluded.estado,
            items       = excluded.items,
            detalle     = excluded.detalle,
            ingerido_at = excluded.ingerido_at
        """,
        (
            salud.fuente,
            salud.fecha.isoformat(),
            salud.estado.value,
            salud.items,
            salud.detalle,
            salud.ingerido_at.isoformat(),
        ),
    )


def ultima_ingesta(conn: sqlite3.Connection) -> tuple[str, date] | None:
    """Fuente y fecha de la ingesta más reciente, o ``None`` si el índice está vacío.

    Devuelve la fuente además de la fecha porque es la que hay que seguir
    alimentando: un índice poblado con la fuente de desarrollo no debe empezar
    a mezclar días del BOE real por su cuenta.
    """
    fila = conn.execute(
        "SELECT fuente, MAX(fecha) AS ultima FROM source_health GROUP BY fuente "
        "ORDER BY ultima DESC LIMIT 1"
    ).fetchone()
    if fila is None or not fila["ultima"]:
        return None
    return fila["fuente"], date.fromisoformat(fila["ultima"])


def fechas_registradas(conn: sqlite3.Connection, fuente: str) -> set[date]:
    """Fechas que ya se han intentado ingerir para esa fuente.

    Incluye los días sin publicación: también están resueltos y no hay que
    volver a pedirlos. Solo se reintentan los que quedaron en error.
    """
    return {
        date.fromisoformat(fila["fecha"])
        for fila in conn.execute(
            "SELECT fecha FROM source_health WHERE fuente = ? AND estado != ?",
            (fuente, EstadoIngesta.ERROR.value),
        )
    }


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #


def obtener_convocatoria(conn: sqlite3.Connection, convocatoria_id: str) -> Convocatoria | None:
    """Recupera una convocatoria por su id, o ``None`` si no está en el índice."""
    fila = conn.execute(
        f"""
        SELECT {_CAMPOS}
        FROM convocatorias c
        LEFT JOIN seguimiento s ON s.convocatoria_id = c.id
        WHERE c.id = ?
        """,
        (convocatoria_id,),
    ).fetchone()
    return _a_convocatoria(fila) if fila else None


def buscar(
    conn: sqlite3.Connection,
    filtros: SearchFilters,
    *,
    sinonimos: Mapping[str, tuple[str, ...]] | None = None,
) -> list[Convocatoria]:
    """Filtro de nivel 1: barato, determinista y sin llamar a ningún modelo.

    Compara palabras clave contra el título normalizado (sin acentos ni
    puntuación), que es lo que trae el sumario. No interpreta requisitos: eso es
    el nivel 2, y lo hace el modelo sobre el texto oficial.

    ``sinonimos`` sustituye la tabla de serie de
    :mod:`convocatorias_mcp.vocabulario`, que traduce términos coloquiales al
    vocabulario administrativo del boletín.
    """
    condiciones: list[str] = []
    params: list[object] = []

    # Todas las palabras tienen que aparecer (AND), pero a cada una le vale
    # cualquiera de sus variantes (OR): "oposiciones" casa con los títulos que
    # dicen "proceso selectivo", que es como lo escribe el BOE de verdad.
    for variantes in expandir_keywords(filtros.keyword_list, sinonimos):
        condiciones.append(
            "(" + " OR ".join("c.titulo_normalizado LIKE ?" for _ in variantes) + ")"
        )
        params.extend(f"%{variante}%" for variante in variantes)

    if filtros.seccion is not None:
        condiciones.append("c.seccion = ?")
        params.append(filtros.seccion.value)

    if filtros.desde is not None:
        condiciones.append("c.fecha_publicacion >= ?")
        params.append(filtros.desde.isoformat())

    if filtros.hasta is not None:
        condiciones.append("c.fecha_publicacion <= ?")
        params.append(filtros.hasta.isoformat())

    if filtros.ambito:
        # El ámbito se guarda como JSON; para el filtro barato basta el texto.
        condiciones.append("LOWER(c.ambito) LIKE ?")
        params.append(f"%{normalize_text(filtros.ambito)}%")

    if filtros.solo_con_plazo_abierto:
        condiciones.append("c.plazo_fin IS NOT NULL AND c.plazo_fin >= ?")
        params.append(date.today().isoformat())

    if not filtros.include_vistas:
        condiciones.append("COALESCE(s.estado, 'nueva') != ?")
        params.append(Estado.VISTA.value)

    if not filtros.include_descartadas:
        condiciones.append("COALESCE(s.estado, 'nueva') != ?")
        params.append(Estado.DESCARTADA.value)

    join_watchlist = ""
    if filtros.watchlist:
        # INNER JOIN: solo las que ha hecho coincidir esa watchlist. Hasta que
        # exista el ranking, esta tabla está vacía y el filtro no devuelve nada.
        join_watchlist = "JOIN convocatoria_watchlists w ON w.convocatoria_id = c.id"
        condiciones.append("w.watchlist = ?")
        params.append(filtros.watchlist)

    where = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""
    params.append(filtros.limite)

    filas = conn.execute(
        f"""
        SELECT {_CAMPOS}
        FROM convocatorias c
        LEFT JOIN seguimiento s ON s.convocatoria_id = c.id
        {join_watchlist}
        {where}
        ORDER BY {ORDEN_CANONICO_SQL}
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_a_convocatoria(fila) for fila in filas]


# --------------------------------------------------------------------------- #
# Conversión entre filas y modelos
# --------------------------------------------------------------------------- #


def _a_fila(convocatoria: Convocatoria, ahora: str) -> dict[str, object]:
    return {
        "id": convocatoria.id,
        "fuente": convocatoria.fuente,
        "identificador": convocatoria.identificador,
        "titulo": convocatoria.titulo,
        "titulo_normalizado": normalize_text(convocatoria.titulo),
        "seccion": convocatoria.seccion.value,
        "seccion_nombre": convocatoria.seccion_nombre,
        "departamento": convocatoria.departamento,
        "epigrafe": convocatoria.epigrafe,
        "control": convocatoria.control,
        "fecha_publicacion": convocatoria.fecha_publicacion.isoformat(),
        "url_html": convocatoria.url_html,
        "url_xml": convocatoria.url_xml,
        "url_pdf": convocatoria.url_pdf,
        "texto_completo": convocatoria.texto_completo,
        "texto_descargado_at": (
            convocatoria.texto_descargado_at.isoformat()
            if convocatoria.texto_descargado_at
            else None
        ),
        "plazo_fin": convocatoria.plazo_fin.isoformat() if convocatoria.plazo_fin else None,
        "plazo_origen": convocatoria.plazo_origen.value,
        "ambito": json.dumps(convocatoria.ambito, ensure_ascii=False),
        "ingerido_at": convocatoria.ingerido_at.isoformat(),
        "actualizado_at": ahora,
    }


def _a_convocatoria(fila: sqlite3.Row) -> Convocatoria:
    return Convocatoria(
        fuente=fila["fuente"],
        identificador=fila["identificador"],
        titulo=fila["titulo"],
        seccion=Seccion(fila["seccion"]),
        seccion_nombre=fila["seccion_nombre"],
        departamento=fila["departamento"],
        epigrafe=fila["epigrafe"],
        control=fila["control"],
        fecha_publicacion=date.fromisoformat(fila["fecha_publicacion"]),
        url_html=fila["url_html"],
        url_xml=fila["url_xml"],
        url_pdf=fila["url_pdf"],
        texto_completo=fila["texto_completo"],
        texto_descargado_at=(
            datetime.fromisoformat(fila["texto_descargado_at"])
            if fila["texto_descargado_at"]
            else None
        ),
        plazo_fin=date.fromisoformat(fila["plazo_fin"]) if fila["plazo_fin"] else None,
        plazo_origen=PlazoOrigen(fila["plazo_origen"]),
        ambito=json.loads(fila["ambito"]) if fila["ambito"] else [],
        estado=Estado(fila["estado"]),
        ingerido_at=datetime.fromisoformat(fila["ingerido_at"]),
    )


def ordenar(convocatorias: Sequence[Convocatoria]) -> list[Convocatoria]:
    """Ordena en memoria con el mismo criterio que :data:`ORDEN_CANONICO_SQL`."""
    return sorted(convocatorias, key=lambda convocatoria: convocatoria.clave_orden)
