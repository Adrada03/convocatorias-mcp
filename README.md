# convocatorias-mcp

Servidor MCP que vigila los boletines oficiales españoles y encuentra
convocatorias que encajan con tu perfil: becas, ayudas, empleo público y
subvenciones. El BOE publica a diario y es inabarcable a mano, así que la gente
pierde convocatorias que le aplicaban.

Dos formas de usarlo: **conversando con Claude** (Desktop o Code) en lenguaje
natural, y como **digest diario por email** con lo nuevo y lo que vence pronto.
Más un dashboard estático para consultar sin instalar nada.

> **Estado: en construcción.** Funciona end-to-end en Claude Desktop contra un
> sumario real del BOE guardado en disco: modelo de dominio, índice SQLite,
> ingesta incremental, servidor MCP con tres tools y el comando `doctor`.
> Pendiente: fuente del BOE en vivo, watchlists y ranking, plazos, digest por
> email y dashboard.

<!-- TODO: GIF de Claude usando el servidor en vivo -->

## Lo que esta herramienta no hace

**No te dice si cumples los requisitos de una convocatoria.** Resume qué pide,
señala coincidencias y discrepancias con tu perfil y enlaza siempre al texto
oficial, que es la única fuente válida. Equivocarse sobre si alguien puede optar
a una ayuda tiene consecuencias reales, así que esa afirmación no la hace el
programa.

Esto no es un descargo de responsabilidad decorativo: está en el modelo de
datos (`AVISO_NO_ELEGIBILIDAD`) para que no se pueda omitir al escribir la
descripción de un tool.

## Cómo funciona: matching en dos niveles

El trabajo se reparte entre lo que un programa hace bien y lo que un modelo
hace bien:

**Nivel 1, en código.** Un filtro barato y determinista sobre el índice local:
palabras clave, sección del boletín y ámbito territorial. Rápido, repetible y
sin llamadas a ningún modelo. Devuelve candidatas.

**Nivel 2, en el modelo.** `get_convocatoria(id)` descarga el texto íntegro y se
lo entrega a Claude para que lea los requisitos. Renta, edad, unidad familiar,
empadronamiento y méritos vienen en prosa jurídica con formatos infinitos:
**no se parsean con expresiones regulares.** Esa comprensión es exactamente
para lo que sirve MCP.

## Instalación y uso

Hace falta Python 3.11 o superior.

```bash
git clone https://github.com/Adrada03/convocatorias-mcp
cd convocatorias-mcp
python -m venv .venv
.venv/Scripts/activate        # en Linux o macOS: source .venv/bin/activate
pip install -e .
```

Puebla el índice local y comprueba que todo está en su sitio:

```bash
convocatorias-mcp ingest
convocatorias-mcp doctor
```

### Conectarlo a Claude Desktop

Añade el servidor a `claude_desktop_config.json`. En Windows está en
`%APPDATA%\Claude\`, y en macOS en
`~/Library/Application Support/Claude/`:

```json
{
  "mcpServers": {
    "convocatorias": {
      "command": "C:\\ruta\\a\\convocatorias-mcp\\.venv\\Scripts\\convocatorias-mcp.exe"
    }
  }
}
```

En macOS o Linux el comando es la ruta al ejecutable del entorno:

```json
{
  "mcpServers": {
    "convocatorias": {
      "command": "/ruta/a/convocatorias-mcp/.venv/bin/convocatorias-mcp"
    }
  }
}
```

Reinicia Claude Desktop y pregúntale en lenguaje natural: *"¿qué convocatorias
de oposiciones hay en el índice?"* o *"búscame subvenciones publicadas esta
semana"*.

Es un **servidor stdio local**: no hay servicio alojado, ni backend, ni claves.
Lo arranca tu Claude en tu máquina y lee un índice SQLite de tu disco. Cualquiera
puede usarlo con su propia cuenta.

### Tools que expone

| Tool | Qué hace |
|---|---|
| `search_convocatorias` | Filtro de nivel 1 sobre el índice: palabras clave, sección, rango de fechas y ámbito. Devuelve metadatos y enlaces, no textos. |
| `get_convocatoria` | Una convocatoria concreta con su texto oficial, para que el modelo lea los requisitos (nivel 2). |
| `source_health` | Hasta dónde llega el índice y si tiene huecos. Sirve para distinguir "no hay nada que encaje" de "esas fechas no están ingeridas". |

Los tools que faltan (`explain_match`, `deadlines_soon`, `mark_solicitada`,
`discard`, `list_solicitudes`, `run_digest`) llegan con el ranking y el digest.
No están registrados: un tool declarado que no funciona es peor que uno que no
existe, porque el modelo lo intentaría.

Toda respuesta lleva el aviso de no elegibilidad y la cita que exige la licencia
del BOE. No es decorativo: hay tests de que ninguna respuesta puede salir sin
ellos.

### Comandos

| Comando | Qué hace |
|---|---|
| `convocatorias-mcp` | Arranca el servidor MCP por stdio. Es como lo invoca Claude Desktop. |
| `convocatorias-mcp ingest [--backfill N] [--forzar]` | Puebla el índice con los sumarios que falten. Incremental: no repite fechas ya resueltas. |
| `convocatorias-mcp doctor [--check-api]` | Revisa la instalación. |

### La fuente de hoy es un sumario guardado

Mientras se implementa la fuente del BOE en vivo, la única fuente registrada es
`FixtureSource`, que lee el sumario real del 17/10/2026 desde
`tests/fixtures/`. Sirve para probar el servidor completo sin red y sin depender
de que el BOE publique hoy.

Eso tiene una consecuencia a tener en cuenta: los ids de esas convocatorias
empiezan por `fixture:` en vez de `boe:`. Cuando entre la fuente real conviene
borrar el índice y volver a ingerir, para no quedarse con la misma convocatoria
duplicada bajo dos ids. La ruta del índice la dice `convocatorias-mcp doctor`.

## Diseño en una frase

Cada boletín es una fuente que implementa la interfaz `BoletinSource`
(`async fetch_dia(fecha) -> list[Convocatoria]`). La ingesta, el índice y el
servidor MCP no conocen ningún boletín concreto, así que añadir uno es crear un
fichero, implementar `fetch_dia()` y registrarlo.

## El pivote: de ofertas de empleo a convocatorias oficiales

Este repo empezó siendo `jobscout-mcp`, un agregador de ofertas de empleo de
portales españoles. El historial de commits lo conserva entero, porque la razón
del cambio es la parte interesante.

Al verificar las fuentes una por una, ninguna resultó viable **con el criterio
de consentimiento explícito** que se describe más abajo:

- **Tecnoempleo** se podía leer técnicamente, pero su `robots.txt` excluye por
  nombre a `ClaudeBot`, `AnthropicBot` y otros agentes de IA. Pasar por la regla
  `*` habría sido una tecnicalidad que no respeta la intención del operador, y
  menos en una herramienta pensada para que la instale cualquiera.
- **InfoJobs** exige aprobación previa como partner.
- **LinkedIn, Indeed y Glassdoor** prohíben el scraping en sus términos.
- **Adzuna** sí tenía API pública y términos compatibles, pero necesita registro
  y credenciales para poder confirmar siquiera que cubre España.

Los boletines oficiales son el caso opuesto: **datos abiertos por ley**, con
licencia de reutilización explícita y una API pública sin autenticación. En vez
de pelear por acceder a datos que alguien no quiere compartir, el proyecto se
mudó a datos que existen precisamente para ser reutilizados. Y el problema que
resuelve es más real: una oferta de empleo la ves en cinco portales, pero una
beca publicada en el BOE un martes no la ve nadie.

La arquitectura sobrevivió al cambio sin tocarse: la interfaz de fuente, el
`SourceError`, el rate limiting y el patrón de fuentes → normalización → índice
→ ranking → digest son los mismos. Solo cambió el dominio.

## Fuentes de datos

| Fuente | Estado | Método | Consentimiento |
|---|---|---|---|
| **BOE** (Boletín Oficial del Estado) | En desarrollo | API de datos abiertos | Datos abiertos con licencia de reutilización |
| Boletines autonómicos (BOCM, BOJA, DOGC…) | Pendiente | Por verificar | Por verificar |

Los autonómicos **no están implementados**: antes hay que comprobar si ofrecen
API de datos abiertos, igual que se verificó el BOE. Es la limitación conocida
más importante — una convocatoria de vivienda protegida de la Comunidad de
Madrid no aparece en el BOE, así que hoy este programa no la encuentra.

### API del BOE

```
GET https://www.boe.es/datosabiertos/api/boe/sumario/{AAAAMMDD}
Accept: application/json
```

Sin autenticación, sin clave y sin registro. Devuelve sumarios **por fecha** y
no permite buscar: de ahí que haga falta un índice local.

Verificado con peticiones reales el 19/10/2026, con el User-Agent de este
proyecto:

- El sumario del 17/10/2026 trae 270 items en 7 secciones (313 KB de JSON).
- Los domingos responden `404` con cuerpo XML. **Eso no es un error**: es
  ausencia de publicación, y se registra como tal para no ensuciar la salud de
  la fuente ni reintentar la fecha para siempre.
- `robots.txt` (494 KB) permite el acceso a la API y no excluye a ningún agente
  de IA.
- Solo admite `GET`; un `POST` devuelve `403`.

### Licencia de reutilización

Los datos vienen de la [sede electrónica de la Agencia Estatal Boletín Oficial
del Estado](https://www.boe.es), y su reutilización se rige por las
[condiciones de reutilización del BOE](https://www.boe.es/informacion/aviso_legal/index.php#reutilizacion),
la licencia tipo aprobada por Resolución de la Agencia de 27 de junio de 2024 al
amparo de la Ley 37/2007 sobre reutilización de la información del sector
público.

Esa licencia permite el uso comercial y no comercial, y obliga a dos cosas que
este proyecto cumple:

1. **Citar la fuente con un enlace a `https://www.boe.es`.** La cita que exige
   la licencia es *"Fuente de los datos: Agencia Estatal Boletín Oficial del
   Estado"*, y aparece en el digest, en el dashboard y en las respuestas del
   servidor. La declara la propia fuente (`cita_requerida`), no la plantilla de
   cada salida, para que cumplirla no dependa de que nadie se acuerde.
2. **No desnaturalizar el sentido de la información.** Por eso el programa no
   interpreta requisitos ni reescribe convocatorias: enlaza al texto oficial y
   deja que lo lea quien tiene que decidir.

Esta herramienta no es un servicio oficial ni está respaldada por el BOE.

## Política de fuentes

Este proyecto **solo consulta fuentes que han dado consentimiento explícito**
para que un tercero lea sus datos. No es una limitación técnica: casi todas las
fuentes descartadas aquí se podrían leer igualmente. Es una decisión de diseño,
y está documentada porque la considero parte del trabajo.

Por orden de preferencia:

1. **Datos abiertos con licencia de reutilización.** El operador publica los
   datos para que se reutilicen y dice bajo qué condiciones.
2. **API pública documentada para terceros.**
3. **Feed RSS o JSON publicado para ser consumido.**
4. **Scraping solo si `robots.txt` permite nuestro caso de uso y no excluye
   agentes de IA.**

Y una regla que manda sobre las cuatro:

> **Si una fuente excluye agentes de IA por nombre, queda descartada, aunque
> técnicamente pudiéramos pasar.**

Esta herramienta canaliza información hacia un asistente de IA. Que nuestro
User-Agent no coincida con los que el operador ha bloqueado es una tecnicalidad
que no respeta su intención. Es un problema de consentimiento, no de carga:
bajar el ritmo de peticiones no lo resolvería.

### Fuentes descartadas

| Fuente | Motivo | Verificado |
|---|---|---|
| **Tecnoempleo** | Su `robots.txt` excluye con `Disallow: /` a `ClaudeBot`, `AnthropicBot`, `anthropic-ai`, `Claude`, `Claude-Web`, `Claude-SearchBot` y `GPTBot`. | 2026-10-19, petición real |
| **LinkedIn, Indeed, Glassdoor** | Sus términos de servicio prohíben el scraping. | Decisión de diseño |
| **InfoJobs** | Su API requiere aprobación previa como partner. | — |
| **Adzuna** | API y términos compatibles, pero exige registro y credenciales. Descartada al pivotar, no por su política. | 2026-10-19 |

### Nota de diseño: `robots.txt` no gobierna una API

`robots.txt` se dirige a rastreadores que descubren contenido siguiendo
enlaces. No es el documento que regula a un cliente de una API publicada para
terceros ni a un reutilizador de datos abiertos. Dos ejemplos verificados:
`api.adzuna.com/robots.txt` es `User-agent: * / Disallow: /`, y
`api.ashbyhq.com/robots.txt` responde `401 Unauthorized`.

Aplicar ahí la comprobación dejaría el proyecto sin fuentes, cumpliendo la letra
de un documento que no va dirigido a nosotros mientras se ignora el contrato que
sí lo está. Así que cada fuente declara **de dónde viene su consentimiento**
(`Consentimiento.DATOS_ABIERTOS`, `API_PUBLICA`, `FEED_PUBLICO` o `ROBOTS`) y la
comprobación de `robots.txt` se aplica solo a las que se apoyan en él. Lo que no
cambia en ninguna fuente: el User-Agent identificado, el rate limiting y la
caché.

## Configuración

### Watchlists

El ranking es opcional. Copia la plantilla y edítala:

```bash
cp watchlists.example.yaml watchlists.yaml
```

```yaml
watchlists:
  - nombre: becas-informatica
    keywords: [informática, software, telecomunicaciones]
    secciones: [III, V.B]
    min_score: 0.5
  - nombre: empleo-publico-tic
    keywords: [analista, programador, sistemas]
    secciones: [II.B]
    ambito: [Madrid, Estatal]
notificacion:
  avisar_plazo_dias: 3
```

Hay varias watchlists, y no un perfil único, porque los criterios de una beca de
informática no se parecen a los de una vivienda protegida: mezclarlos daría un
score sin significado. El digest agrupa por watchlist.

Secciones que traen convocatorias: **II.B** oposiciones y concursos, **III**
bases reguladoras de becas y subvenciones, **V.A** licitaciones públicas y
**V.B** otros anuncios oficiales. Se aceptan tanto la etiqueta romana (`III`)
como el código interno del BOE (`3`).

`watchlists.yaml` está en `.gitignore`: tus criterios son tuyos y el repo solo
versiona la plantilla. Sin el fichero el servidor funciona igual, ordenando por
fecha.

### Digest por email

Opcional, en `.env` (también en `.gitignore`):

```bash
cp .env.example .env
```

Con Gmail hace falta una **contraseña de aplicación**, no la del correo: activa
la verificación en dos pasos y genérala en
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).

Sin SMTP configurado el digest no falla: se escribe a fichero y se avisa.

### El comando `doctor`

Este servidor lo lanza Claude Desktop en segundo plano, así que cuando algo está
mal configurado no hay ningún mensaje de error a la vista. `doctor` es el sitio
donde mirar:

```bash
convocatorias-mcp doctor --check-api
```

```
[ ok ] Python: 3.12.10
[ ok ] watchlists.yaml: 2: becas-informatica (4 keywords, 2 secciones), empleo-publico-tic (3 keywords, 1 sección)
[ ok ] índice: C:\Users\...\convocatorias-mcp\convocatorias-index.sqlite3
[warn] contenido del índice: vacío; ejecuta 'convocatorias-mcp ingest' para poblarlo
[warn] SMTP: sin configurar; el digest se escribirá a fichero en vez de enviarse
[ ok ] API del BOE: sumario del 2026-10-19: HTTP 200, 371.771 bytes
```

Revisa las watchlists (y avisa si tienen un typo, en vez de ignorarlo), el
estado del índice con sus huecos, el SMTP y, con `--check-api`, la conectividad
real con el BOE. Devuelve código de salida 1 si algo impide funcionar.

**Nunca imprime credenciales ni datos personales**, ni completos ni parciales:
su salida está pensada para pegarse en un issue. De las claves dice la longitud,
de las watchlists cuenta las keywords sin listarlas, y la dirección del digest
no se imprime ni enmascarada.

## Acceso educado a las fuentes

Verificado con tests, no solo documentado:

- User-Agent identificado y honesto: `convocatorias-mcp/0.1.0 (+URL del repo)`.
  No se imita ningún navegador.
- Retardo mínimo de 1,5 s entre peticiones al mismo dominio, **compartido entre
  fuentes** y no por instancia, para que un backfill concurrente no pueda
  duplicar el ritmo.
- Si un sitio declara un `Crawl-delay` mayor, gana el del sitio.
- Índice local agresivo: una fecha ya ingerida no se vuelve a pedir, y un texto
  completo ya descargado tampoco.
- Fechas en zona horaria `Europe/Madrid`, que es cuando publica el BOE.

## Desarrollo

```bash
python -m venv .venv
.venv/Scripts/activate        # en Linux o macOS: source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

Ningún test toca la red: las fuentes HTTP se prueban con `respx` y los parsers
con sumarios reales del BOE guardados en `tests/fixtures/`.

## Licencia

MIT. Ver [LICENSE](LICENSE). Los datos del BOE se reutilizan bajo
[sus condiciones de reutilización](https://www.boe.es/informacion/aviso_legal/index.php#reutilizacion).
