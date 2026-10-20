# convocatorias-mcp

Servidor MCP que vigila los boletines oficiales españoles y encuentra
convocatorias que encajan con tu perfil: becas, ayudas, empleo público y
subvenciones. El BOE publica a diario y es inabarcable a mano, así que la gente
pierde convocatorias que le aplicaban.

Se usa **conversando con Claude** (Desktop o Code) en lenguaje natural, y trae
un dashboard estático para consultar sin instalar nada.

> **Estado: en construcción.** Funciona end-to-end contra el BOE real: el
> índice local tiene unas 15.000 convocatorias de los últimos 90 días, con
> servidor MCP de ocho tools, ingesta incremental, ranking por watchlists y
> dashboard. Pendiente: activar el despliegue del dashboard.

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
palabras clave y sección del boletín. Rápido, repetible y
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

### Conectarlo a Claude Code

```bash
claude mcp add --transport stdio --scope user convocatorias -- /ruta/a/convocatorias-mcp/.venv/Scripts/convocatorias-mcp.exe
```

En macOS o Linux el ejecutable está en `.venv/bin/convocatorias-mcp` en vez de
`.venv/Scripts/convocatorias-mcp.exe`. Comprueba la conexión con:

```bash
claude mcp list
```

`convocatorias` debe aparecer como `Connected`.

### Conectarlo a Claude Desktop

Las versiones recientes de Claude Desktop ya no leen
`claude_desktop_config.json` a mano: instalan servidores MCP locales como
**extensiones de escritorio** (`.mcpb`), de un solo clic.

> **El `.mcpb` todavía no está publicado en una release.** El bundle ya se
> construye y se ha verificado desempaquetándolo fuera de la carpeta donde se
> genera (el primer intento solo arrancaba en la máquina que lo construyó, y
> se retiró en vez de dejarlo descargable; la causa está en
> [Construir el `.mcpb`](#construir-el-mcpb-por-qué-un-venv-no-vale)).
> Hasta que se publique, se construye con las instrucciones de esa sección, o
> se usa **Claude Code**, arriba, que no necesita empaquetado.

Una vez tengas el `.mcpb`, se instala con cualquiera de estas tres opciones:

- Haz doble clic en el archivo `.mcpb`.
- Arrástralo a la ventana de Claude Desktop.
- **Settings → Extensions → Advanced settings → Install Extension…** y
  selecciónalo.

Irá sin firmar (no hay certificado de desarrollador detrás), así que Claude
Desktop avisará de ello al instalarlo.

> Si usas una versión de Claude Desktop **anterior** a las extensiones `.mcpb`
> (antes de 2025), todavía puede valer la configuración manual en
> `claude_desktop_config.json` (`%APPDATA%\Claude\` en Windows,
> `~/Library/Application Support/Claude/` en macOS):
> ```json
> {
>   "mcpServers": {
>     "convocatorias": {
>       "command": "C:\\ruta\\a\\convocatorias-mcp\\.venv\\Scripts\\convocatorias-mcp.exe"
>     }
>   }
> }
> ```
> En versiones nuevas este archivo simplemente no se lee: si lo editas y no
> pasa nada, es señal de que te hace falta el `.mcpb` de arriba, no de que lo
> hiciste mal.

Una vez conectado (por Code o por Desktop), pregúntale en lenguaje natural:
*"¿qué convocatorias de oposiciones hay en el índice?"* o *"búscame
subvenciones publicadas esta semana"*.

Es un **servidor stdio local**: no hay servicio alojado, ni backend, ni claves.
Lo arranca tu Claude en tu máquina y lee un índice SQLite de tu disco. Cualquiera
puede usarlo con su propia cuenta.

### Construir el `.mcpb`: por qué un venv no vale

El `.mcpb` no se versiona en el repo ni se genera a mano, y el primer intento
—empaquetar un venv y apuntar el manifest a su `convocatorias-mcp.exe`—
resultó estar roto de una forma que no se ve: **funcionaba en la máquina
donde se construyó y en ninguna otra.** Merece la pena contarlo porque el
fallo es silencioso.

Un venv **no es relocalizable**. Dos cosas lo atan a su sitio:

1. **El `.exe` del entry point lleva una ruta absoluta.** pip genera los
   console-scripts con un shebang embebido apuntando al intérprete del venv
   que los creó:
   ```
   #!C:\...\build\mcpb\server\venv\Scripts\python.exe
   ```
   Instalado en otra ruta, el `.exe` reenvía igualmente al directorio de
   build. En la máquina donde se construyó, eso existe y arranca; fuera, no.
2. **El venv no contiene el runtime.** Ni `Lib/` ni `python3xx.dll` viven
   dentro: `pyvenv.cfg` apunta a la instalación base de Python de esa
   máquina, que no viaja en el bundle.

El efecto combinado es que la extensión instalada no ejecutaba ni siquiera su
propia copia de `site-packages`, sino la del directorio de build. Se detectó
mirando el árbol de procesos, no los logs, porque nada fallaba.

El empaquetado actual evita las dos trampas:

```bash
pip install --target mcpb/server/lib .   # arbol plano y movible, sin -e
npm install -g @anthropic-ai/mcpb
mcpb pack mcpb dist/convocatorias-mcp.mcpb
```

`pip install --target` produce un árbol de paquetes sin rutas absolutas, y el
manifest invoca el módulo (`python -m convocatorias_mcp.cli` con `PYTHONPATH`)
en vez de un console-script, que es lo que embebía el shebang. El bundle se
prueba desempaquetándolo en otra carpeta y arrancándolo allí: si solo se
prueba donde se construyó, el fallo no aparece.

**El runtime no va dentro**, así que hace falta Python 3.11 o superior en el
PATH.

Ese requisito **no se declara en `compatibility.runtimes.python`**, y la razón
merece contarse. Ese campo no es documentación: Claude Desktop lo usa como
puerta, y para evaluarla busca Python en las ubicaciones habituales de
instalación. Con un Python instalado fuera de ellas —por ejemplo directamente
en la carpeta del usuario— no lo encuentra, **deshabilita el botón de instalar
y no hay forma de continuar**, aunque `python --version` responda
perfectamente en una terminal y la extensión fuese a funcionar. Pasó en la
máquina de desarrollo.

Así que el requisito vive en `long_description`, que se muestra en el mismo
diálogo de instalación. No es esconderlo: es ponerlo donde informa a una
persona en vez de donde bloquea a quien sí lo cumple. La alternativa real es
empotrar el intérprete, que daría instalación de un clic sin requisitos; son
~11 MB más y un bundle por plataforma. Hoy `compatibility.platforms` ya dice
`win32`, porque los wheels compilados (`lxml`, `pydantic_core`,
`cryptography`) son `cp312-win_amd64` y habría que construir uno por sistema.

Dos detalles que solo aparecen al ejecutarlo:

- **`pip install --target` no procesa los ficheros `.pth`**, y `pywin32` —que
  llega como dependencia de `mcp`— reparte `pywintypes` por `win32/lib`. Sin
  añadir esas carpetas al `PYTHONPATH`, el servidor muere al importar. Por eso
  el manifest declara cuatro rutas y no una.
- **Las watchlists no viajan en el bundle**, ni deben: son datos personales.
  El manifest las pide como `user_config` de tipo `file` y las pasa en
  `CONVOCATORIAS_WATCHLISTS_PATH`, así que Claude Desktop genera la casilla al
  instalar. Sin ella el servidor funciona, pero ordena por fecha sin puntuar.

### Tools que expone

| Tool | Qué hace |
|---|---|
| `search_convocatorias` | Filtro de nivel 1 sobre el índice: palabras clave, sección, fechas y watchlist. Devuelve metadatos y enlaces, no textos. |
| `get_convocatoria` | Una convocatoria con su texto oficial completo, para que el modelo lea los requisitos (nivel 2). La primera vez lo descarga del boletín y lo cachea; solo bajo demanda, nunca las 15.000 por adelantado. |
| `explain_match` | Por qué una convocatoria encaja (o no) con cada watchlist, con el desglose del score por dimensiones. |
| `deadlines_soon` | Lo que vence en los próximos N días, de las convocatorias con plazo determinado. |
| `mark_solicitada` | Anota que la has solicitado, con notas y fecha. Es tu registro de solicitudes. |
| `discard` | Descarta una convocatoria, con su motivo. Desaparece de búsquedas y avisos. |
| `list_solicitudes` | Lo que has solicitado, con sus notas. Opcionalmente también lo descartado. |
| `source_health` | Hasta dónde llega el índice, su antigüedad y si tiene huecos. Sirve para distinguir "no hay nada que encaje" de "esas fechas no están ingeridas". |

Falta `run_digest`, que llega con el digest por email. No está registrado: un
tool declarado que no funciona es peor que uno que no existe, porque el modelo
lo intentaría y se llevaría un error.

Toda respuesta lleva el aviso de no elegibilidad y la cita que exige la licencia
del BOE. No es decorativo: hay tests de que ninguna respuesta puede salir sin
ellos.

### El ranking

Con watchlists configuradas, los resultados se ordenan por afinidad. El score
sale de cuatro dimensiones, con estos pesos por defecto:

| Dimensión | Peso | Qué mira |
|---|---|---|
| Título | 0,55 | Palabras clave de la watchlist en el título. La señal más directa. |
| Sección | 0,25 | Que la sección sea una de las que vigila, o al menos una de las que traen convocatorias. |
| Departamento | 0,15 | Palabras clave en el departamento o el epígrafe. Suma cuando acierta, no resta cuando no. |
| ~~Ámbito~~ | — | **Inactiva.** El BOE no publica el campo: está vacío en las 14.935. No resta (solo puntúa cuando ambos lados lo traen), pero nunca suma. |

Tres decisiones que importan:

- **El scoring no mira el texto completo.** Puntuar con el texto obligaría a
  descargar 15.000 documentos y rompería el reparto en dos niveles: el score
  ordena candidatas, y la profundidad la pone el modelo sobre las pocas que
  suben.
- **Una sección no vigilada es una exclusión, no un cero.** Si vigilas II.B y
  III, una licitación de V.A no es "poco afín": es que no la has pedido.
- **Las keywords de una watchlist son un OR.** Un perfil con cuatro intereses
  no busca convocatorias que los mezclen todos; en una búsqueda explícita, en
  cambio, las palabras que escribes se exigen todas.

Los pesos viven en `ranking.Pesos` para poder afinarlos con el uso sin tocar la
lógica. Y `explain_match` enseña el desglose, que es la forma de entender por
qué sale lo que sale:

```
empleo-publico-tic: score 0.5416
  titulo         peso=0.55  cumple=0.3333 -> 0.1833
    1 de 3 palabras clave en el título: sistemas.
  departamento   peso=0.15  cumple=0.0    -> 0.0
    Ninguna de las 3 palabras clave aparece en el departamento o el epígrafe.
  seccion        peso=0.25  cumple=1.0    -> 0.25
    La sección II.B está entre las que vigila esta watchlist (II.B).
```

El score mide **afinidad temática, no elegibilidad**. Que algo puntúe 1,0 no
dice nada sobre si cumples sus requisitos.

### Los plazos, y por qué muchos faltan

`plazo_fin` se extrae del texto oficial cuando se descarga, y solo si el texto
lo dice de forma explícita e inequívoca: la frase tiene que hablar de un plazo
**y** de presentar solicitudes, y dar una sola fecha. Si no, queda en
`desconocido` y se dice con palabras.

Sobre una muestra real de 18 extractos de convocatorias de la sección V.B:

- **5 declaran una fecha absoluta** y se extraen bien.
- **2 usan plazos relativos** ("veinte días hábiles desde la publicación"), que
  no se calculan: haría falta el calendario de festivos nacionales,
  autonómicos y locales, y equivocarse en un día es equivocarse.
- **11 no declaran plazo** en el extracto, porque remiten a las bases.

Esa proporción es la razón de que `deadlines_soon` avise de que su lista no es
exhaustiva. Un plazo equivocado es peor que no tener plazo, porque alguien se
fiaría de él.

Durante el desarrollo el extractor sí se equivocó una vez, y merece la pena
contarlo: cogió el `31 de diciembre de 2028` de la frase *"la beca no podrá
superar la fecha límite de 31 de diciembre de 2028"*. Era la duración de la
beca, no su plazo de solicitud. Por eso ahora se exige que la frase hable de
presentar algo, y por eso hay un test con ese texto exacto.

### El índice se pone al día solo

Trabajar con datos viejos sin saberlo es peor que esperar unos segundos, así que
el servidor comprueba la frescura del índice en cada consulta:

- **Hasta 7 días de atraso** (configurable con `CONVOCATORIAS_AUTO_INGEST_DIAS`):
  ingiere los días que falten antes de responder. Siete días son como mucho
  siete peticiones.
- **Más de 7 días**: no bloquea la consulta. Responde con lo que haya e incluye
  un aviso de que el índice está desactualizado y de que hay que ejecutar
  `convocatorias-mcp ingest`, porque un backfill de semanas no cabe en el
  tiempo de una respuesta.
- Solo se intenta una vez por sesión: si la red está caída, no se hace esperar
  al usuario en cada consulta.

Y todas las respuestas, estén al día o no, dicen `indice_cubre_hasta` y
`dias_de_atraso`. El objetivo es que el modelo **nunca afirme que no hay
convocatorias cuando lo que pasa es que no ha mirado**.

### El vocabulario del boletín

Los títulos del BOE están en lenguaje administrativo. Una oposición no se titula
"oposiciones": se titula *"Resolución de 8 de octubre de 2026, de la
Subsecretaría, por la que se convoca proceso selectivo para ingreso..."*. Buscar
"oposiciones" sobre 2.255 convocatorias de la sección II.B devolvía cero, y era
correcto.

La solución principal está en **la descripción del tool**, que enseña al modelo
qué contiene cada sección y cómo traducir la intención del usuario: el cliente
es un LLM, y eso es su trabajo, no el de un filtro `LIKE`. Como red de
seguridad hay además una tabla corta de sinónimos (`oposiciones` → `proceso
selectivo`, `pruebas selectivas`...), ampliable con un `sinonimos.yaml` propio:

```bash
cp sinonimos.example.yaml sinonimos.yaml
```

Cada clave del fichero sustituye la de serie, así que se puede corregir una
entrada y no solo añadir.

### Comandos

| Comando | Qué hace |
|---|---|
| `convocatorias-mcp` | Arranca el servidor MCP por stdio. Es como lo invoca Claude Desktop. |
| `convocatorias-mcp ingest [--backfill N] [--fuente X] [--forzar]` | Puebla el índice con los sumarios que falten. Incremental: no repite fechas ya resueltas, pero sí reintenta las que fallaron. Imprime una línea por día. |
| `convocatorias-mcp export [--dias N] [--todo] [--salida RUTA]` | Genera el JSON que consume el dashboard. |
| `convocatorias-mcp doctor [--check-api]` | Revisa la instalación. |

### La fuente de desarrollo

Además del BOE hay una fuente `fixture` que lee sumarios guardados en
`tests/fixtures/`. No toca la red, así que sirve para desarrollar y para que
cualquiera pruebe el proyecto entero sin depender de que el BOE publique hoy:

```bash
convocatorias-mcp ingest --fuente fixture
```

Sus ids empiezan por `fixture:` en vez de `boe:`, porque el prefijo de la
fuente forma parte del id. Si habías poblado el índice con ella, bórralo antes
de ingerir el BOE real para no quedarte con la misma convocatoria bajo dos ids.
La ruta del índice la dice `convocatorias-mcp doctor`.

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
| **BOE** (Boletín Oficial del Estado) | Funcionando | API de datos abiertos | Datos abiertos con licencia de reutilización |
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

- El sumario del 17/10/2026 trae 271 items en 7 secciones (313 KB de JSON).
- Los domingos responden normalmente `404`, con cuerpo XML aunque pidas JSON.
  **Eso no es un error**: es ausencia de publicación, y se registra como tal
  para no ensuciar la salud de la fuente ni reintentar la fecha para siempre.
- **Pero no todos los domingos.** En el backfill de 90 días, 11 domingos
  devolvieron `404` y 2 devolvieron `200` con contenido: el BOE publicó
  ediciones extraordinarias el 26/07 y el 09/08/2026, por la emergencia de
  incendios y por controles fronterizos.
- Esos dos sumarios llegan además **con otra forma**: un nivel `texto` extra
  entre `seccion` y `departamento`, y entre `departamento` y `epigrafe`. El
  parser maneja las dos, y resultó que el envoltorio también aparecía en los
  sumarios normales (en la sección V.C), donde estaba perdiendo un anuncio en
  silencio.
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
  - nombre: empleo-publico
    keywords: [proceso selectivo, pruebas selectivas]
    secciones: [II.B]
  - nombre: becas
    keywords: [beca, becas, matrícula]
    secciones: [III, V.B]
notificacion:
  avisar_plazo_dias: 3
```

Las keywords van en el vocabulario del boletín, no en el tuyo: sobre un índice
real de 14.935 convocatorias, `analista` y `programador` aparecen **0 veces**,
y `proceso selectivo` 262. Y no hay `min_score` en el ejemplo a propósito: su
defecto es 0, y subirlo es lo que más resultados hace desaparecer sin avisar
(ver abajo).

`convocatorias-mcp doctor` cuenta cuántas casa cada watchlist contra tu índice
y avisa si alguna casa casi nada, distinguiendo si la culpa es de las keywords
o del `min_score`. Una watchlist mal configurada no da ningún error: callada,
se confunde con "no hay nada que te encaje".

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
[ ok ] API del BOE: sumario del 2026-10-19: HTTP 200, 371.771 bytes
```

Revisa las watchlists (y avisa si tienen un typo, en vez de ignorarlo), el
estado del índice con sus huecos y, con `--check-api`, la conectividad real con
el BOE. Devuelve código de salida 1 si algo impide funcionar.

**Nunca imprime credenciales ni datos personales**, ni completos ni parciales:
su salida está pensada para pegarse en un issue. De las claves dice la longitud,
de las watchlists cuenta las keywords sin listarlas, y la dirección del digest
no se imprime ni enmascarada.

## El dashboard

Una página HTML estática, sin backend y sin framework, para consultar el índice
sin instalar nada. Se genera así:

```bash
convocatorias-mcp export
python -m http.server -d dashboard 8000
```

Y se abre en [localhost:8000](http://localhost:8000). Hace falta el servidor
porque abriendo el fichero con doble clic el navegador bloquea la lectura de
`data.json`; si pasa, la propia página lo explica y da el comando.

Permite buscar por texto, filtrar por watchlist, sección, estado y rango de
fechas, y ordenar por fecha, por plazo o por afinidad. Lo que vence en siete
días o menos va marcado en rojo, y lo del mes, en ámbar. Funciona en móvil y
sigue el modo claro u oscuro del sistema.

### Qué se exporta, y qué no

**Solo metadatos.** Nunca textos completos: el texto oficial vive en el BOE y
la página enlaza a él. Ni siquiera se exporta la URL, porque se deriva del
identificador; eran 940 KB de repetir la misma cadena 15.000 veces.

Los tamaños, medidos sobre el índice real de 14.935 convocatorias:

| Variante | Crudo | Gzip |
|---|---|---|
| Todo, claves legibles | 7,31 MB | 926 KB |
| Todo, claves cortas y sin campos vacíos | 4,42 MB | 747 KB |
| **30 días + coincidencias (por defecto)** | **1,66 MB** | **~290 KB** |

Por defecto se recorta a los últimos 30 días **más todo lo que coincida con
alguna watchlist**, y la página lo dice en su cabecera junto a la fecha hasta
la que llegan los datos. Una beca de hace dos meses que encaja con tu perfil es
exactamente lo que no se puede perder por recortar.

El recorte no es por la red —gzipped el índice entero son 747 KB y GitHub Pages
comprime— sino porque 4,4 MB de JSON son 15.000 objetos que un móvil tiene que
parsear y mantener en memoria. Con `--todo` se exporta completo, que en
escritorio va sobrado.

### Las watchlists del sitio publicado

El dashboard desplegado usa `watchlists.demo.yaml`, con intereses inventados
(investigación, empleo público TIC, ayudas al estudio y vivienda), para que se
vea el ranking y los chips de afinidad funcionando sobre datos reales. La
propia página avisa de que son de ejemplo.

Las watchlists de verdad viven en `watchlists.yaml`, que no se versiona: los
criterios de cada uno dicen bastante de uno mismo, y eso no se publica.

Una de ellas lleva `min_score: 0.6` a propósito. Sus keywords incluyen
"becas", que tira de los sinónimos y arrastra "ayuda" y "subvención",
palabras que inundan las secciones III y V.B: sin ese mínimo, esa watchlist
casaría una de cada seis convocatorias del BOE. Es el ejemplo de para qué
sirve `min_score`.

### Despliegue

`.github/workflows/pages.yml` está escrito pero **no activo**: solo se puede
lanzar a mano, y publicar requiere además activar Pages en el repositorio. El
sitio irá en `convocatorias.alvaroadrada.dev` vía CNAME.

Antes de activarlo hay una decisión pendiente, y está anotada en el propio
workflow: `dashboard/data.json` está en `.gitignore` porque se genera desde el
índice local, así que hay que elegir si se versiona, se genera en CI con una
ingesta programada o se sube como artefacto.

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
