# jobscout-mcp

Servidor MCP que busca ofertas de empleo en varios portales españoles a la vez,
las deduplica y las ordena por afinidad con tu perfil. Pensado para usarse desde
Claude Desktop o Claude Code en lenguaje natural: *"búscame ofertas de backend
Python en Madrid publicadas esta semana"*.

> **Estado: en construcción.** Este README se completa al final del desarrollo
> con instalación, configuración, diagrama de arquitectura, tabla de fuentes y
> la sección de limitaciones y decisiones de diseño.

## Diseño en una frase

Cada portal es un adaptador que implementa la interfaz `JobSource`
(`async search(filters) -> list[JobListing]`). El agregador, la caché y el
servidor MCP no conocen ningún portal concreto, así que añadir una fuente nueva
es crear un fichero, implementar `search()` y registrarlo.

## Licencia

MIT. Ver [LICENSE](LICENSE).
