# AcortadorLinks

Descargador de enlaces con acortadores: resuelve cadenas de redirecciones
(esperas, formularios y contadores de publicidad) y descarga el archivo final.
Incluye una interfaz web local sencilla y una CLI.

## Características

- Resuelve wrappers, `wp-safelink`, AdLinkFly, `mediafire`, `meta refresh` y
  formularios automáticos, respetando los tiempos de espera.
- Caché local de páginas finales estables (`--refresh` para ignorarla).
- Interfaz web en `127.0.0.1` con cuadro de texto para pegar los enlaces,
  selector nativo de carpeta del PC y barras de progreso.
- CLI con paralelismo, destino configurable y nombres de archivo únicos.

## Requisitos

- Python 3.10 o superior.
- `requests` y `beautifulsoup4` (ver `requirements.txt`).
- Para el selector de carpeta nativo: `tkinter` (viene con Python en Linux).

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## Uso: interfaz web

```bash
python3 app.py                 # abre http://127.0.0.1:8765 en el navegador
python3 app.py --port 9000     # otro puerto
python3 app.py --no-browser    # no abrir el navegador
python3 app.py -v              # registrar cada petición HTTP
```

Pega los enlaces (uno por línea), pulsa **Seleccionar carpeta** y luego
**Descargar**. Verás una barra de progreso global y otra del archivo en curso,
un log en vivo y un botón para cancelar. La interfaz escucha solo en
`127.0.0.1`, así que no queda expuesta a la red.

## Uso: línea de comandos

```bash
python3 resolve_links.py "https://softurl.in/XXXX"
python3 resolve_links.py -f links.txt -o downloads
python3 resolve_links.py -f links.txt --info            # solo el enlace final
python3 resolve_links.py -f links.txt -o downloads -v   # traza de saltos
```

Opciones útiles: `-j N` (paralelismo), `-o CARPETA` (destino), `--force`
(sobrescribir), `--skip-existing`, `--refresh` (ignorar caché),
`--max-hops`, `--max-wait`, `--wait-buffer`, `--full-chain`.

`links.txt` admite una URL por línea (las que empiezan por `#` se ignoran).

## Estructura

| Ruta | Contenido |
| --- | --- |
| `app.py` | Servidor local y API de la interfaz web |
| `index.html` | Página de la interfaz (cuadro de enlaces y progreso) |
| `resolve_links.py` | Motor de resolución y descarga (también CLI) |
| `tests/` | Tests `unittest` y fixtures HTML |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Protección frente a cookies

Los archivos descargados pueden contener cookies de sesión y no deben subirse
nunca. El repo lo protege en dos capas:

- `.gitignore` ignora `cookies/`, `*cookie*.txt` y las exportaciones típicas de
  extensiones (`26_PK.txt`, `9_US.txt`, …).
- `.githooks/pre-commit` bloquea el commit si algún archivo preparado parece un
  volcado de cookies (formato Netscape o nombres como `NetflixId`, `A3PSID`).

El hook está activado con `git config core.hooksPath .githooks`; si clonas el
repo en otro equipo, actívalo con ese mismo comando. Para saltarlo de forma
puntual: `git commit --no-verify`.

## Notas

- No se imprimen tokens completos: los enlaces se muestran con `redact_url`.
- El archivo se descarga primero a un `.part` y se renombra al terminar, así no
  quedan archivos incompletos si la descarga falla o se cancela.
