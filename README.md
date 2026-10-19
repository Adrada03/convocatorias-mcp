# convocatorias-mcp

Servidor MCP que busca ofertas de empleo en varias fuentes a la vez, las
deduplica y las ordena por afinidad con tu perfil. Pensado para usarse desde
Claude Desktop o Claude Code en lenguaje natural: *"búscame ofertas de backend
Python en Madrid publicadas esta semana"*.

> **Estado: en construcción.** Este README se completa al final del desarrollo
> con instalación, configuración, diagrama de arquitectura y la sección de
> limitaciones. La política de fuentes de más abajo ya es definitiva.

## Diseño en una frase

Cada fuente es un adaptador que implementa la interfaz `JobSource`
(`async search(filters) -> list[JobListing]`). El agregador, la caché y el
servidor MCP no conocen ninguna fuente concreta, así que añadir una es crear un
fichero, implementar `search()` y registrarlo.

## Política de fuentes

Este proyecto **solo consulta fuentes que han dado consentimiento explícito**
para que un tercero lea sus datos. No es una limitación técnica: casi todas las
fuentes descartadas aquí se podrían leer igualmente. Es una decisión de diseño,
y está documentada porque la considero parte del trabajo.

Por orden de preferencia:

1. **API pública documentada para terceros.** El operador publica un contrato,
   unos términos y, normalmente, unas credenciales. El consentimiento es
   explícito e inequívoco.
2. **Feed RSS o JSON publicado para ser consumido.** Existe para que alguien lo
   lea desde fuera; leerlo es su propósito.
3. **Scraping solo si `robots.txt` permite nuestro caso de uso y no excluye
   agentes de IA.**

Y una regla que manda sobre las tres:

> **Si una fuente excluye agentes de IA por nombre, queda descartada, aunque
> técnicamente pudiéramos pasar.**

El motivo: esta herramienta canaliza ofertas hacia un asistente de IA. Que
nuestro User-Agent no coincida con los que el operador ha bloqueado es una
tecnicalidad que no respeta su intención, y menos todavía en una herramienta
pensada para que la instale cualquiera. Es un problema de consentimiento, no de
carga: bajar el ritmo de peticiones no lo resolvería.

### Portales descartados

| Fuente | Motivo | Verificado |
|---|---|---|
| **Tecnoempleo** | Su `robots.txt` excluye con `Disallow: /` a `ClaudeBot`, `AnthropicBot`, `anthropic-ai`, `Claude`, `Claude-Web`, `Claude-SearchBot` y `GPTBot`. La regla `*` sí permitiría nuestro User-Agent, pero aplica la regla de arriba. | 2026-10-19, petición real |
| **LinkedIn, Indeed, Glassdoor** | Sus términos de servicio prohíben el scraping, y además tienen anti-bot agresivo. Ni se intenta. | Decisión de diseño |
| **InfoJobs** | Su API requiere aprobación previa como partner. Pendiente de comprobar si existe un feed público que cumpla el criterio; si no, queda descartada. | Pendiente |
| **Talentoteca** | Pendiente de evaluar con este criterio. | Pendiente |

### Nota de diseño: `robots.txt` no gobierna una API

`robots.txt` se dirige a rastreadores que descubren contenido siguiendo
enlaces. No es el documento que regula a un cliente de API al que el operador
le ha emitido credenciales y unos términos de uso. Los dos hosts de API
verificados lo ilustran:

- `api.adzuna.com/robots.txt` es `User-agent: * / Disallow: /`
- `api.ashbyhq.com/robots.txt` responde `401 Unauthorized`

Aplicar ahí la comprobación de `robots.txt` dejaría el proyecto sin ninguna
fuente de API, cumpliendo la letra de un documento que no va dirigido a
nosotros mientras se ignora el contrato que sí lo está. Así que cada fuente
declara **de dónde viene su consentimiento**, y la comprobación de `robots.txt`
se aplica solo a las que se apoyan en él. Lo que no cambia en ninguna fuente:
el User-Agent identificado, el rate limiting y la caché.

## Configuración

### Credenciales de Adzuna

Adzuna es la fuente principal: un agregador, así que con una sola integración
legítima se cubre mucho volumen. Su API es gratuita con registro.

1. Regístrate en [developer.adzuna.com/signup](https://developer.adzuna.com/signup).
   Te dan un `app_id` y un `app_key`.
2. Copia la plantilla y rellena tus claves:

   ```bash
   cp .env.example .env
   ```

3. Comprueba que han quedado bien:

   ```bash
   convocatorias-mcp doctor --check-api
   ```

`.env` está en `.gitignore` y no entra nunca al repositorio. El comando
`doctor` **nunca imprime el valor de una credencial**: solo dice si está
configurada y cuántos caracteres tiene, para que su salida se pueda pegar en un
issue sin filtrar nada.

Límites del plan gratuito: **25 peticiones por minuto y 250 al día**. Sobran
para uso personal, pero son la razón por la que la caché no es una optimización
sino parte del funcionamiento normal.

Sin credenciales el servidor arranca igual: Adzuna se marca como `skipped` en
`source_status` y el resto de fuentes funciona.

### El comando `doctor`

Este servidor lo lanza Claude Desktop en segundo plano, así que cuando algo está
mal configurado no hay ningún mensaje de error a la vista. `doctor` es el sitio
donde mirar:

```bash
convocatorias-mcp doctor
```

Revisa la versión de Python, el `.env`, las credenciales, el `profile.yaml` (y
avisa si tiene un typo, en vez de ignorarlo en silencio) y si la carpeta de la
caché es escribible. Sin `--check-api` no sale a la red, así que no gasta cuota.
Devuelve código de salida 1 si encuentra algo que impida funcionar.

### Perfil de afinidad

El ranking es opcional. Si copias `profile.example.yaml` a `profile.yaml` y lo
rellenas, los resultados se ordenan por afinidad con tu perfil; si no, se
ordenan por fecha. `profile.yaml` también está en `.gitignore`: tus datos son
tuyos, y el repo solo versiona la plantilla.

## Licencia

MIT. Ver [LICENSE](LICENSE).
