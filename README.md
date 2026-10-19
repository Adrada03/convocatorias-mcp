# jobscout-mcp

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

## Licencia

MIT. Ver [LICENSE](LICENSE).
