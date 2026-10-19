# Fixtures de HTML real

HTML capturado de los portales reales para los tests de los parsers. Ningún
test de la suite hace peticiones de red: estos ficheros son la entrada.

Se guardan tal cual llegaron, sin recortar ni reformatear, para que los tests
fallen cuando un portal cambie su estructura de verdad. Cuando eso pase, se
vuelve a capturar el fichero y se actualiza esta tabla.

Todas las capturas se hicieron con el User-Agent del proyecto
(`jobscout-mcp/0.1.0 (+https://github.com/Adrada03/jobscout-mcp)`) y
respetando `robots.txt` y el retardo entre peticiones.

| Fichero | Origen | Capturado | Qué cubre |
|---|---|---|---|
| `tecnoempleo_search_python.html` | `GET /ofertas-trabajo/?te=python&pr=madrid` | 2026-10-19 | Página de resultados (30 ofertas). Ojo: `pr=madrid` es inválido y el portal lo ignora, así que estos resultados **no** están filtrados por provincia. Incluye 10 ofertas sin empresa y 10 con salario. |
| `tecnoempleo_search_python_pr263.html` | `GET /ofertas-trabajo/?te=python&pr=263` | 2026-10-19 | Los mismos resultados filtrados por Madrid (código `263`). Contiene los casos `Madrid` y `Madrid (Híbrido)` que no aparecen en la captura sin filtrar. |
| `tecnoempleo_detail.html` | `GET /senior-python-developer/batch-control-m/rf-6a50102692c9239f864a` | 2026-10-19 | Página de detalle de una oferta. |
