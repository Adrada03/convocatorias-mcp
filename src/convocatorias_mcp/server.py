"""Servidor MCP por transporte stdio.

Expone el índice local como tools para Claude. El reparto de trabajo es el del
matching en dos niveles: estos tools hacen el filtro barato y determinista
(nivel 1) y le entregan al modelo el texto oficial para que lea los requisitos
(nivel 2).

Las descripciones de los tools están escritas para que un modelo elija bien
cuándo usar cada uno y qué parámetros pasar. En MCP eso es parte del oficio, no
cosmética: una descripción vaga se traduce en llamadas inútiles.

**Nada se escribe en stdout.** El transporte stdio usa stdout para el protocolo,
así que un ``print`` suelto rompería la sesión. Los avisos van a stderr.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from convocatorias_mcp import __version__
from convocatorias_mcp.config import ConfigError, Settings, hoy_boletin, load_watchlists
from convocatorias_mcp.db import estado_del_indice, open_index
from convocatorias_mcp.index import (
    buscar,
    convocatorias_que_vencen,
    guardar_texto,
    listar_por_estado,
    marcar_seguimiento,
    obtener_convocatoria,
    ultima_ingesta,
)
from convocatorias_mcp.ingest import ingerir
from convocatorias_mcp.models import (
    AVISO_NO_ELEGIBILIDAD,
    Convocatoria,
    Estado,
    SearchFilters,
    Seccion,
    Seguimiento,
    WatchlistsConfig,
)
from convocatorias_mcp.plazos import extraer_plazo
from convocatorias_mcp.ranking import puntuar, rankear
from convocatorias_mcp.sources import FUENTES, crear_fuente
from convocatorias_mcp.sources.base import SourceError
from convocatorias_mcp.vocabulario import cargar_sinonimos

__all__ = ["construir_servidor", "main"]

CITA_BOE = "Fuente de los datos: Agencia Estatal Boletín Oficial del Estado (https://www.boe.es)"

INSTRUCCIONES = f"""
Vigila los boletines oficiales españoles y encuentra convocatorias: becas,
ayudas, empleo público y subvenciones.

{AVISO_NO_ELEGIBILIDAD}

Flujo recomendado: busca candidatas con search_convocatorias y, cuando una
parezca encajar, pide get_convocatoria para leer sus requisitos en el texto
oficial. No afirmes que la persona cumple o no cumple: resume lo que pide la
convocatoria, señala coincidencias y discrepancias con lo que sepas de su
perfil, y enlaza siempre al texto oficial.
""".strip()

SOLO_LECTURA = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
# Los tools de estado escriben, pero no destruyen nada y repetirlos deja el
# mismo resultado: eso es lo que anuncian sus anotaciones.
ESCRITURA = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


@contextmanager
def _indice(settings: Settings) -> Iterator[sqlite3.Connection]:
    """Abre el índice para una sola llamada.

    Una conexión por llamada en vez de una compartida: SQLite ata su conexión al
    hilo que la crea, y abrir un fichero local es barato.
    """
    conn = open_index(settings.index_path)
    try:
        yield conn
    finally:
        conn.close()


def _resumir(convocatoria: Convocatoria) -> dict[str, Any]:
    """Convierte una convocatoria en el dicto que ve el modelo.

    Se devuelven los metadatos y los enlaces, nunca el texto completo: para eso
    está ``get_convocatoria``, y meterlo aquí llenaría la respuesta de prosa
    jurídica que el modelo no ha pedido todavía.
    """
    return {
        "id": convocatoria.id,
        "identificador_oficial": convocatoria.identificador,
        "titulo": convocatoria.titulo,
        "fecha_publicacion": convocatoria.fecha_publicacion.isoformat(),
        "seccion": convocatoria.seccion.value,
        "seccion_descripcion": convocatoria.seccion.descripcion,
        "departamento": convocatoria.departamento,
        "epigrafe": convocatoria.epigrafe,
        "ambito": convocatoria.ambito,
        "plazo_fin": convocatoria.plazo_fin.isoformat() if convocatoria.plazo_fin else None,
        "plazo": (
            "desconocido (no se ha podido determinar; consulta el texto oficial)"
            if convocatoria.plazo_desconocido
            else convocatoria.plazo_fin.isoformat()
            if convocatoria.plazo_fin
            else "sin plazo de cierre"
        ),
        "estado": convocatoria.estado.value,
        "watchlists": convocatoria.watchlists,
        "score": convocatoria.score,
        "url_oficial": convocatoria.url_html,
        "url_pdf": convocatoria.url_pdf,
    }


@dataclass(frozen=True)
class Frescura:
    """Hasta cuándo llega el índice, y si eso es un problema.

    Existe para que el modelo nunca diga "no hay convocatorias" cuando lo que
    pasa es que no ha mirado: cada respuesta lleva esta información.
    """

    fuente: str | None = None
    ultima_fecha: date | None = None
    dias_atraso: int | None = None
    desactualizado: bool = False
    aviso: str | None = None
    dias_puestos_al_dia: int = 0
    convocatorias_nuevas: int = 0

    def como_dict(self) -> dict[str, Any]:
        datos: dict[str, Any] = {
            "indice_cubre_hasta": self.ultima_fecha.isoformat() if self.ultima_fecha else None,
            "dias_de_atraso": self.dias_atraso,
            "indice_desactualizado": self.desactualizado,
        }
        if self.aviso:
            datos["aviso_indice"] = self.aviso
        if self.dias_puestos_al_dia:
            datos["puesta_al_dia"] = (
                f"Se han ingerido {self.dias_puestos_al_dia} día(s) que faltaban, "
                f"con {self.convocatorias_nuevas} convocatorias nuevas, antes de responder."
            )
        return datos


async def _poner_al_dia(ajustes: Settings, intentados: set[str]) -> Frescura:
    """Comprueba la frescura del índice y, si falta poco, lo pone al día.

    Tres casos, y el reparto importa:

    * **Al día**: no hace nada.
    * **Atraso pequeño** (hasta ``auto_ingest_dias``): ingiere los días que
      faltan antes de responder. Son segundos, y evita que alguien trabaje con
      datos viejos sin saberlo.
    * **Atraso grande**: no bloquea la consulta. Responde con lo que hay y
      avisa de que hay que ejecutar ``convocatorias-mcp ingest``, porque un
      backfill de semanas no cabe en el tiempo de una respuesta.

    Solo se intenta una vez por proceso y fuente: si la red está caída, no tiene
    sentido reintentar en cada consulta y dejar al usuario esperando siempre.
    """
    hoy = hoy_boletin()
    with _indice(ajustes) as conn:
        ultima = ultima_ingesta(conn)

    if ultima is None:
        # Sin registro de ingesta no se sabe hasta cuándo se ha comprobado la
        # fuente. Si aun así hay datos, se dice hasta dónde llegan: es menos
        # información que una fecha comprobada, pero mejor que un null.
        with _indice(ajustes) as conn:
            estado = estado_del_indice(conn)
        if estado.convocatorias == 0:
            return Frescura(
                desactualizado=True,
                aviso=(
                    "El índice local está vacío: no se ha ingerido nada todavía, así que "
                    "no se puede saber si hay convocatorias. Dile al usuario que ejecute "
                    "'convocatorias-mcp ingest'."
                ),
            )
        return Frescura(
            ultima_fecha=estado.ultima_publicacion,
            desactualizado=True,
            aviso=(
                "El índice tiene datos pero ningún registro de ingesta, así que no se "
                "puede saber si están al día. Puede haber convocatorias más recientes "
                "que no están aquí."
            ),
        )

    fuente, ultima_fecha = ultima
    atraso = (hoy - ultima_fecha).days
    if atraso <= 0:
        return Frescura(fuente=fuente, ultima_fecha=ultima_fecha, dias_atraso=max(atraso, 0))

    def desactualizada(extra: str = "") -> Frescura:
        return Frescura(
            fuente=fuente,
            ultima_fecha=ultima_fecha,
            dias_atraso=atraso,
            desactualizado=True,
            aviso=(
                f"El índice llega al {ultima_fecha.isoformat()} y hoy es "
                f"{hoy.isoformat()}: faltan {atraso} día(s). Puede haber convocatorias "
                "más recientes que no están aquí, así que no afirmes que no hay nada. "
                f"Dile al usuario que ejecute 'convocatorias-mcp ingest'.{extra}"
            ),
        )

    if ajustes.auto_ingest_dias <= 0 or atraso > ajustes.auto_ingest_dias:
        return desactualizada()
    if fuente in intentados:
        return desactualizada(" Ya se intentó poner al día en esta sesión y no se pudo.")

    intentados.add(fuente)
    try:
        source = crear_fuente(fuente)
    except KeyError:
        return desactualizada(f" La fuente {fuente!r} ya no está registrada.")

    conn = open_index(ajustes.index_path)
    try:
        resumen = await ingerir(conn, source, desde=ultima_fecha + timedelta(days=1), hasta=hoy)
        nueva_ultima = ultima_ingesta(conn)
    finally:
        conn.close()
        await source.aclose()

    if resumen.errores:
        return desactualizada(
            f" Al ponerlo al día fallaron {len(resumen.errores)} día(s): {resumen.errores[0][1]}"
        )

    fecha_final = nueva_ultima[1] if nueva_ultima else ultima_fecha
    return Frescura(
        fuente=fuente,
        ultima_fecha=fecha_final,
        dias_atraso=(hoy - fecha_final).days,
        dias_puestos_al_dia=resumen.dias_pedidos,
        convocatorias_nuevas=resumen.convocatorias,
    )


async def _descargar_texto(
    ajustes: Settings, convocatoria: Convocatoria
) -> tuple[Convocatoria, str | None]:
    """Descarga el texto oficial y lo cachea en el índice.

    Devuelve la convocatoria y, si algo salió mal, una nota para el modelo. Un
    fallo aquí **no es un error del tool**: la convocatoria y su enlace siguen
    siendo útiles, y lo peor que puede pasar es que el modelo resuma requisitos
    que no ha leído. Por eso la nota se lo dice explícitamente.
    """
    enlace = f"Lee la convocatoria en {convocatoria.url_html} y no deduzcas sus requisitos."

    try:
        fuente = crear_fuente(convocatoria.fuente)
    except KeyError:
        return convocatoria, (
            f"La fuente {convocatoria.fuente!r} ya no está registrada, así que no se "
            f"puede descargar el texto. {enlace}"
        )

    try:
        con_texto = await fuente.fetch_texto(convocatoria)
    except SourceError as exc:
        return convocatoria, f"No se pudo descargar el texto oficial: {exc.message}. {enlace}"
    finally:
        await fuente.aclose()

    if not con_texto.texto_descargado:
        return con_texto, (
            f"La fuente {convocatoria.fuente!r} no descarga textos completos. {enlace}"
        )

    # El plazo sale del texto, así que se extrae aquí y se guarda de una vez.
    # Si no se puede determinar queda en None: nunca se inventa una fecha.
    plazo, origen = extraer_plazo(con_texto.texto_completo, publicado=con_texto.fecha_publicacion)
    if plazo is not None:
        con_texto = con_texto.model_copy(update={"plazo_fin": plazo, "plazo_origen": origen})

    with _indice(ajustes) as conn:
        guardar_texto(conn, con_texto)
    return con_texto, None


def construir_servidor(settings: Settings | None = None) -> MCPServer:
    """Monta el servidor con sus tools. Separado de :func:`main` para los tests."""
    ajustes = settings or Settings.from_env()
    # Se cargan una vez al arrancar: el fichero del usuario no cambia en
    # caliente y leerlo en cada búsqueda sería gratuito pero absurdo.
    sinonimos = cargar_sinonimos(ajustes.sinonimos_path)
    # Un intento de puesta al día por proceso y fuente: si la red está
    # caída, no se hace esperar al usuario en cada consulta.
    intentos_de_puesta_al_dia: set[str] = set()

    def watchlists() -> WatchlistsConfig | None:
        """Watchlists del usuario, releídas en cada uso.

        A diferencia de los sinónimos, este fichero se edita a menudo
        mientras se afina el perfil, y obligar a reiniciar Claude Desktop
        para probar un cambio sería una mala experiencia.
        """
        try:
            return load_watchlists(ajustes.watchlists_path)
        except ConfigError:
            # Un fichero roto no debe tumbar la búsqueda: doctor lo explica.
            return None

    server = MCPServer(
        name="convocatorias",
        title="Convocatorias oficiales",
        version=__version__,
        instructions=INSTRUCCIONES,
    )

    @server.tool(
        name="search_convocatorias",
        title="Buscar convocatorias",
        annotations=SOLO_LECTURA,
    )
    async def search_convocatorias(
        keywords: str | None = None,
        seccion: str | None = None,
        desde: date | None = None,
        hasta: date | None = None,
        watchlist: str | None = None,
        include_vistas: bool = False,
        limite: int = 25,
    ) -> dict[str, Any]:
        """Busca convocatorias en el índice local de boletines oficiales.

        Devuelve metadatos y enlaces, no el texto completo: para leer los
        requisitos de una convocatoria concreta, llama después a
        get_convocatoria.

        IMPORTANTE: los títulos del BOE están escritos en lenguaje
        administrativo, no coloquial. Una oposición nunca se titula
        "oposiciones": se titula "Resolución de 8 de octubre de 2026, de la
        Subsecretaría, por la que se convoca proceso selectivo para ingreso en
        el Cuerpo...". Traducir la intención del usuario a ese vocabulario es
        tu trabajo, y el filtro más potente casi siempre es la sección, no las
        palabras.

        Qué contiene cada sección:

        - "II.B": empleo público. Procesos selectivos, pruebas selectivas,
          concursos-oposición, concursos de traslados, listas de admitidos y
          excluidos, tribunales. Aquí está todo lo que la gente llama
          "oposiciones".
        - "III": bases reguladoras y convocatorias de becas, ayudas,
          subvenciones, premios y convenios. Aquí están las becas de verdad,
          con sus requisitos.
        - "V.B": extractos de convocatorias de subvenciones y otros anuncios
          oficiales. Suele ser la versión corta de algo publicado en III, y es
          la sección con más volumen.
        - "V.A": contratación pública. Licitaciones, adjudicaciones y
          formalizaciones de contratos. Útil para autónomos y empresas, no para
          quien busca empleo o una beca.
        - "I", "II.A", "IV", "V.C": normativa general, nombramientos,
          administración de justicia y anuncios particulares. Rara vez son
          convocatorias a las que alguien pueda optar.

        Cómo construir una buena consulta:

        - "quiero oposiciones de informática" -> seccion="II.B" y
          keywords="informatica" (o "sistemas", "telecomunicaciones"). No pases
          "oposiciones" como keyword: no aparece en los títulos.
        - "becas para estudiar un máster" -> seccion="III" y keywords="becas".
        - "¿qué ha salido esta semana?" -> solo desde, sin keywords.
        - "ayudas para autónomos en Madrid" -> seccion="III" y
          keywords="autonomos", y revisa el ámbito en los resultados en vez de
          filtrar por él, que no siempre viene.
        - Si una búsqueda con varias palabras no devuelve nada, prueba con
          menos: las keywords se exigen TODAS.

        Hay una tabla pequeña de sinónimos que traduce algunos términos
        coloquiales ("oposiciones" -> "proceso selectivo"), pero es una red de
        seguridad y no cubre el idioma entero: no te apoyes en ella.

        Args:
            keywords: Palabras que deben aparecer TODAS en el título, sin
                distinguir mayúsculas ni acentos. Usa el vocabulario del
                boletín, no el del usuario.
            seccion: Sección del boletín, como "II.B" o "III". Es el filtro más
                útil; úsalo siempre que la intención del usuario esté clara.
            desde: Fecha de publicación mínima, en formato AAAA-MM-DD.
            hasta: Fecha de publicación máxima, en formato AAAA-MM-DD.
            watchlist: Nombre de una watchlist del usuario para quedarte solo
                con lo que ella hace coincidir. Si el usuario dice "lo de
                siempre" o "mis becas", mira primero qué watchlists tiene en la
                respuesta de source_health.
            include_vistas: Por defecto se omiten las ya enviadas en un digest.
                Ponlo a true si el usuario quiere revisar también esas.
            limite: Máximo de resultados, entre 1 y 200.
        """
        filtros = SearchFilters(
            keywords=keywords,
            seccion=Seccion.from_text(seccion) if seccion else None,
            desde=desde,
            hasta=hasta,
            # Sin 'ambito': SearchFilters sigue soportándolo para cuando una
            # fuente lo publique, pero el BOE no lo hace, así que el tool no
            # lo expone. Ver el comentario de SearchFilters.ambito.
            include_vistas=include_vistas,
            limite=limite,
        )
        # Antes de buscar, al día: responder con datos viejos sin avisar es
        # peor que tardar unos segundos.
        frescura = await _poner_al_dia(ajustes, intentos_de_puesta_al_dia)

        config = watchlists()
        seleccionada = config.get(watchlist) if (config and watchlist) else None
        if watchlist and seleccionada is None:
            nombres = [w.nombre for w in config.watchlists] if config else []
            return {
                "total": 0,
                "resultados": [],
                "error": (
                    f"No hay ninguna watchlist llamada {watchlist!r}. "
                    + (
                        f"Las configuradas son: {', '.join(nombres)}."
                        if nombres
                        else "El usuario no tiene watchlists configuradas."
                    )
                ),
            }

        config_activa = bool(config and not config.is_empty)
        # Con ranking se piden más candidatas de las que se van a devolver: si
        # no, se ordenaría solo el puñado más reciente y una convocatoria muy
        # afín de hace dos semanas no llegaría a verse nunca.
        margen = (
            filtros.model_copy(update={"limite": min(200, max(filtros.limite * 8, 50))})
            if config_activa
            else filtros
        )

        with _indice(ajustes) as conn:
            resultados = buscar(conn, margen, sinonimos=sinonimos, watchlist=seleccionada)
            estado = estado_del_indice(conn)

        # El score ordena candidatas; la profundidad la pone el modelo sobre las
        # pocas que suben. Por eso se puntúa con el título, la sección y el
        # departamento, nunca con el texto completo.
        if config_activa:
            solo = WatchlistsConfig(watchlists=[seleccionada]) if seleccionada else config
            resultados = rankear(
                resultados,
                solo,
                sinonimos=sinonimos,
                solo_coincidentes=bool(seleccionada),
            )[: filtros.limite]

        respuesta: dict[str, Any] = {
            "total": len(resultados),
            "resultados": [_resumir(convocatoria) for convocatoria in resultados],
            "aviso": AVISO_NO_ELEGIBILIDAD,
            "cita_obligatoria": CITA_BOE,
        }
        # Se mira si hay convocatorias, no si hay dias registrados: un indice
        # con datos pero sin registro de ingesta no esta vacio, y decirle al
        # modelo que lo esta le haria afirmar que no existe nada.
        respuesta.update(frescura.como_dict())
        if estado.convocatorias == 0:
            respuesta["indice_vacio"] = (
                "El índice local está vacío: no hay nada que buscar todavía. "
                "El usuario tiene que ejecutar 'convocatorias-mcp ingest' para poblarlo."
            )
        return respuesta

    @server.tool(
        name="get_convocatoria",
        title="Leer una convocatoria",
        annotations=SOLO_LECTURA,
    )
    async def get_convocatoria(id: str) -> dict[str, Any]:
        """Devuelve una convocatoria con su TEXTO OFICIAL COMPLETO.

        Llámalo cuando una convocatoria de search_convocatorias parezca encajar
        y haga falta leer sus requisitos: plazos, titulación, renta, méritos,
        baremos. Esos requisitos vienen en prosa jurídica y hay que leerlos; no
        se deducen del título.

        La primera vez descarga el texto del boletín y lo guarda, así que la
        siguiente llamada es instantánea. Pídelo solo de las convocatorias que
        de verdad interesen: son 15.000 en el índice y descargarlas todas sería
        abusar de una fuente pública.

        Al resumir: di qué pide la convocatoria, señala qué coincide y qué no
        con lo que sepas del perfil del usuario, y enlaza siempre al texto
        oficial. **No afirmes que cumple o no cumple los requisitos**: esa
        conclusión no es tuya, y equivocarse tiene consecuencias reales.

        Args:
            id: El campo "id" de un resultado de search_convocatorias, con el
                formato "boe:BOE-A-2026-21635".
        """
        with _indice(ajustes) as conn:
            convocatoria = obtener_convocatoria(conn, id)

        if convocatoria is None:
            return {
                "encontrada": False,
                "error": (
                    f"No hay ninguna convocatoria con id {id!r} en el índice local. "
                    "Comprueba el id con search_convocatorias."
                ),
            }

        nota: str | None = None
        if not convocatoria.texto_descargado:
            convocatoria, nota = await _descargar_texto(ajustes, convocatoria)

        respuesta = _resumir(convocatoria)
        respuesta["encontrada"] = True
        respuesta["texto_completo"] = convocatoria.texto_completo
        respuesta["aviso"] = AVISO_NO_ELEGIBILIDAD
        respuesta["cita_obligatoria"] = CITA_BOE
        if nota:
            respuesta["nota_texto"] = nota
        return respuesta

    @server.tool(
        name="source_health",
        title="Estado del índice y las fuentes",
        annotations=SOLO_LECTURA,
    )
    async def source_health() -> dict[str, Any]:
        """Informa de hasta dónde llega el índice local y si hay huecos.

        Úsalo cuando una búsqueda no devuelva nada y haya que distinguir "no hay
        convocatorias que encajen" de "el índice no tiene esas fechas", o cuando
        el usuario pregunte si los datos están al día.
        """
        frescura = await _poner_al_dia(ajustes, intentos_de_puesta_al_dia)
        with _indice(ajustes) as conn:
            estado = estado_del_indice(conn)

        return {
            "convocatorias_indexadas": estado.convocatorias,
            "dias_registrados": estado.dias_registrados,
            "dias_con_publicacion": estado.dias_ok,
            "dias_sin_publicacion": estado.dias_sin_publicacion,
            "dias_con_error": estado.dias_con_error,
            "primera_fecha": (estado.primera_fecha.isoformat() if estado.primera_fecha else None),
            "ultima_fecha_comprobada": (
                estado.ultima_fecha.isoformat() if estado.ultima_fecha else None
            ),
            "ultima_publicacion_indexada": (
                estado.ultima_publicacion.isoformat() if estado.ultima_publicacion else None
            ),
            "huecos": [dia.isoformat() for dia in estado.huecos[:20]],
            "huecos_totales": len(estado.huecos),
            "hoy": hoy_boletin().isoformat(),
            "resumen": estado.resumen(),
            **frescura.como_dict(),
            "fuentes": [
                {
                    "nombre": clase.name,
                    "descripcion": clase.display_name,
                    "metodo": clase.method,
                    "licencia": clase.licencia_url,
                }
                for clase in FUENTES.values()
            ],
            "cita_obligatoria": CITA_BOE,
        }

    @server.tool(name="explain_match", title="Por qué encaja", annotations=SOLO_LECTURA)
    async def explain_match(id: str, watchlist: str | None = None) -> dict[str, Any]:
        """Explica por qué una convocatoria encaja (o no) con las watchlists.

        Úsalo cuando el usuario pregunte "¿por qué me sale esto?" o "¿por qué no
        me sale lo que busco?". Devuelve el desglose del score por dimensiones:
        palabras clave en el título, sección, departamento y ámbito.

        Ojo con lo que significa: el score mide **afinidad temática**, no
        elegibilidad. Que puntúe alto no dice nada sobre si el usuario cumple
        los requisitos; eso está en el texto y se lee con get_convocatoria.

        Args:
            id: El campo "id" de un resultado de search_convocatorias.
            watchlist: Para explicar solo una. Sin esto se explican todas.
        """
        config = watchlists()
        if config is None or config.is_empty:
            return {
                "explicado": False,
                "error": (
                    "El usuario no tiene watchlists configuradas, así que no hay nada "
                    "contra lo que puntuar. Puede crearlas copiando "
                    "watchlists.example.yaml a watchlists.yaml."
                ),
            }

        with _indice(ajustes) as conn:
            convocatoria = obtener_convocatoria(conn, id)
        if convocatoria is None:
            return {
                "explicado": False,
                "error": f"No hay ninguna convocatoria con id {id!r} en el índice local.",
            }

        if watchlist:
            elegida = config.get(watchlist)
            if elegida is None:
                return {
                    "explicado": False,
                    "error": (
                        f"No hay ninguna watchlist llamada {watchlist!r}. Las "
                        f"configuradas son: "
                        f"{', '.join(w.nombre for w in config.watchlists)}."
                    ),
                }
            objetivo = [elegida]
        else:
            objetivo = list(config.watchlists)

        explicaciones: list[dict[str, Any]] = []
        for lista in objetivo:
            match = puntuar(convocatoria, lista)
            if match is None:
                if not lista.acepta_seccion(convocatoria.seccion):
                    motivo = (
                        f"La sección {convocatoria.seccion.value} no está entre las que "
                        f"vigila esta watchlist."
                    )
                else:
                    motivo = f"El score no llega al mínimo que pide ({lista.min_score})."
                explicaciones.append(
                    {"watchlist": lista.nombre, "coincide": False, "motivo": motivo}
                )
                continue
            explicaciones.append(
                {
                    "watchlist": lista.nombre,
                    "coincide": True,
                    "score": match.total,
                    "keywords_encontradas": match.keywords_encontradas,
                    "keywords_ausentes": match.keywords_ausentes,
                    "componentes": [
                        {
                            "dimension": componente.nombre,
                            "peso": componente.peso,
                            "cumplimiento": componente.ratio,
                            "puntos": componente.puntos,
                            "detalle": componente.detalle,
                        }
                        for componente in match.componentes
                    ],
                }
            )

        return {
            "explicado": True,
            "convocatoria": _resumir(convocatoria),
            "watchlists": explicaciones,
            "nota": (
                "El score mide afinidad temática con lo que trae el sumario (título, "
                "sección y departamento). No mide elegibilidad."
            ),
            "aviso": AVISO_NO_ELEGIBILIDAD,
        }

    @server.tool(name="deadlines_soon", title="Plazos que vencen", annotations=SOLO_LECTURA)
    async def deadlines_soon(dias: int = 7, limite: int = 25) -> dict[str, Any]:
        """Convocatorias con el plazo abierto que cierran dentro de N días.

        Úsalo para "¿qué se me acaba pronto?" o para un repaso semanal.

        IMPORTANTE: solo aparecen las convocatorias cuyo plazo se ha podido
        determinar leyendo su texto. Muchas lo expresan como "veinte días
        hábiles desde la publicación", que no se puede calcular sin el
        calendario de festivos, y **esas no salen aquí**. Esta lista no es
        exhaustiva: no le digas al usuario que no le vence nada más.

        Args:
            dias: Ventana en días naturales desde hoy. Por defecto 7.
            limite: Máximo de resultados.
        """
        frescura = await _poner_al_dia(ajustes, intentos_de_puesta_al_dia)
        hoy = hoy_boletin()
        with _indice(ajustes) as conn:
            proximas = convocatorias_que_vencen(conn, dias=dias, hoy=hoy, limite=limite)
            con_plazo = conn.execute(
                "SELECT COUNT(*) AS n FROM convocatorias WHERE plazo_fin IS NOT NULL"
            ).fetchone()["n"]
            total = conn.execute("SELECT COUNT(*) AS n FROM convocatorias").fetchone()["n"]

        respuesta: dict[str, Any] = {
            "dias": dias,
            "hoy": hoy.isoformat(),
            "total": len(proximas),
            "resultados": [
                {
                    **_resumir(convocatoria),
                    "dias_restantes": convocatoria.dias_hasta_plazo(hoy),
                }
                for convocatoria in proximas
            ],
            "cobertura_de_plazos": (
                f"Solo {con_plazo} de las {total} convocatorias del índice tienen plazo "
                "determinado, por dos razones: el plazo se extrae del texto oficial y "
                "ese texto solo se descarga cuando se pide con get_convocatoria, y "
                "muchas convocatorias lo expresan en días hábiles desde la publicación, "
                "que no se puede calcular sin el calendario de festivos. Esta lista NO "
                "es exhaustiva: no le digas al usuario que no le vence nada más."
            ),
            "aviso": AVISO_NO_ELEGIBILIDAD,
            "cita_obligatoria": CITA_BOE,
        }
        respuesta.update(frescura.como_dict())
        return respuesta

    def _marcar(seguimiento: Seguimiento) -> dict[str, Any]:
        with _indice(ajustes) as conn:
            guardado = marcar_seguimiento(conn, seguimiento)
            convocatoria = obtener_convocatoria(conn, seguimiento.convocatoria_id)

        if not guardado or convocatoria is None:
            return {
                "guardado": False,
                "error": (
                    f"No hay ninguna convocatoria con id {seguimiento.convocatoria_id!r} "
                    "en el índice local, así que no se ha anotado nada. Comprueba el id "
                    "con search_convocatorias."
                ),
            }
        return {
            "guardado": True,
            "estado": convocatoria.estado.value,
            "convocatoria": _resumir(convocatoria),
        }

    @server.tool(name="mark_solicitada", title="Marcar como solicitada", annotations=ESCRITURA)
    async def mark_solicitada(
        id: str, notas: str | None = None, fecha: date | None = None
    ) -> dict[str, Any]:
        """Anota que el usuario ha solicitado una convocatoria.

        Sirve de registro de solicitudes, que después se consulta con
        list_solicitudes. Llámalo cuando el usuario diga que la ha pedido, que
        la ha presentado o que ya la ha entregado.

        Args:
            id: El campo "id" de un resultado de search_convocatorias.
            notas: Lo que el usuario quiera recordar: por dónde la presentó, qué
                documentación envió, un número de registro.
            fecha: Fecha de la solicitud en formato AAAA-MM-DD. Por defecto hoy.
        """
        return _marcar(
            Seguimiento(
                convocatoria_id=id,
                estado=Estado.SOLICITADA,
                notas=notas,
                fecha_solicitud=fecha or hoy_boletin(),
            )
        )

    @server.tool(name="discard", title="Descartar", annotations=ESCRITURA)
    async def discard(id: str, motivo: str | None = None) -> dict[str, Any]:
        """Descarta una convocatoria para que no vuelva a aparecer.

        Llámalo cuando el usuario diga que algo no le interesa o no le aplica.
        Las descartadas se omiten de las búsquedas y de los avisos de plazo.

        Guarda el motivo si el usuario lo da: sirve para entender después por
        qué se descartó, y para afinar sus watchlists.

        Args:
            id: El campo "id" de un resultado de search_convocatorias.
            motivo: Por qué no le interesa, con las palabras del usuario.
        """
        return _marcar(Seguimiento(convocatoria_id=id, estado=Estado.DESCARTADA, motivo=motivo))

    @server.tool(name="list_solicitudes", title="Solicitudes presentadas", annotations=SOLO_LECTURA)
    async def list_solicitudes(incluir_descartadas: bool = False) -> dict[str, Any]:
        """Lista las convocatorias que el usuario ha marcado como solicitadas.

        Úsalo para "¿a qué me he apuntado?" o para repasar lo presentado.
        Devuelve también las notas que se guardaron con cada una.

        Args:
            incluir_descartadas: Añade además las descartadas, con su motivo.
        """
        with _indice(ajustes) as conn:
            solicitadas = listar_por_estado(conn, Estado.SOLICITADA)
            descartadas = listar_por_estado(conn, Estado.DESCARTADA) if incluir_descartadas else []

        def como_dict(par: tuple[Convocatoria, Seguimiento]) -> dict[str, Any]:
            convocatoria, seguimiento = par
            datos = _resumir(convocatoria)
            datos["notas"] = seguimiento.notas
            datos["motivo"] = seguimiento.motivo
            datos["fecha_solicitud"] = (
                seguimiento.fecha_solicitud.isoformat() if seguimiento.fecha_solicitud else None
            )
            return datos

        respuesta: dict[str, Any] = {
            "total": len(solicitadas),
            "solicitadas": [como_dict(par) for par in solicitadas],
            "cita_obligatoria": CITA_BOE,
        }
        if incluir_descartadas:
            respuesta["descartadas"] = [como_dict(par) for par in descartadas]
        return respuesta

    return server


def main() -> int:
    """Arranca el servidor MCP por stdio."""
    construir_servidor().run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
