# Fixtures reales del BOE

Respuestas capturadas de la API real para los tests de los parsers. Ningún test
de la suite hace peticiones de red: estos ficheros son la entrada.

Se guardan tal cual llegaron, sin recortar ni reformatear, para que los tests
fallen cuando la API cambie de verdad. Cuando eso pase, se vuelve a capturar el
fichero y se actualiza esta tabla.

Capturados con el User-Agent del proyecto
(`convocatorias-mcp/0.1.0 (+https://github.com/Adrada03/convocatorias-mcp)`) y
respetando el retardo entre peticiones.

| Fichero | Origen | Capturado | Qué cubre |
|---|---|---|---|
| `boe_sumario_20261017.json` | `GET /datosabiertos/api/boe/sumario/20261017` | 2026-10-19 | Sumario completo de un día con publicación: 271 items en 7 secciones (`1`, `2A`, `2B`, `3`, `4`, `5B`, `5C`). Incluye items sin epígrafe y sin número de expediente, y `url_pdf` como objeto en vez de cadena. |
| `boe_sumario_sin_publicacion.json` | `GET /datosabiertos/api/boe/sumario/20261018` | 2026-10-19 | Respuesta de un domingo: HTTP 404 con cuerpo **XML**, aunque la petición pida JSON. Es ausencia de publicación, no un error. |
| `boe_documento.xml` | `GET /diario_boe/xml.php?id=BOE-A-2026-21635` | 2026-10-19 | Documento individual con su texto completo, para el nivel 2 del matching. Trae metadatos que el sumario no da: `rango`, `fecha_disposicion` y `analisis`. |

## Detalles verificados que estos ficheros documentan

- **Los códigos de sección no son los romanos.** El sumario usa `"2B"`, no
  `"II.B"`. La traducción en los dos sentidos vive en `Seccion`.
- **`url_pdf` es un objeto** con `texto` (la URL), `szBytes`, `szKBytes`,
  `pagina_inicial` y `pagina_final`.
- **No hay hora de publicación** en ninguna parte: el sumario trae
  `"20261017"` y el XML del documento,
  `<fecha_publicacion>20261017</fecha_publicacion>`.
