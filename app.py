#!/usr/bin/env python3
"""Interfaz web local para resolver enlaces y descargar los archivos.

Sirve una página HTML en 127.0.0.1 con un cuadro de texto para pegar los
enlaces, un botón que abre el diálogo nativo para elegir la carpeta de destino
y barras de progreso de la descarga.

Uso:
    python3 app.py
    python3 app.py --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "index.html"

# Permite importar resolve_links.py aunque se lance desde otra carpeta.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from resolve_links import (  # noqa: E402
    CACHE_FILE,
    DEFAULT_CACHE_TTL,
    LinkResolver,
    ResolutionCache,
    ResolveError,
    redact_url,
)

MAX_LOG_LINES = 400

# Diálogo nativo de selección de carpeta (tkinter en un subproceso aislado).
FOLDER_DIALOG_SCRIPT = """
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception:
    sys.exit(3)
try:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(title="Selecciona la carpeta de descarga")
    root.destroy()
except Exception:
    sys.exit(4)
print(path or "")
"""


def parse_links(text: str) -> list[str]:
    """Convierte el texto pegado en una lista de URLs únicas y válidas."""
    urls: list[str] = []
    vistos: set[str] = set()
    for numero, linea in enumerate(text.splitlines(), 1):
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        for token in linea.split():
            if not token.startswith(("http://", "https://")):
                raise ResolveError(
                    f"Enlace no válido en la línea {numero}: {redact_url(token)}"
                )
            if token not in vistos:
                vistos.add(token)
                urls.append(token)
    if not urls:
        raise ResolveError("No se indicó ningún enlace.")
    return urls


def elegir_carpeta() -> str:
    """Abre el diálogo nativo de carpetas y devuelve la ruta elegida."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise ResolveError(
            "No hay entorno gráfico disponible para abrir el selector de carpetas."
        )
    try:
        proceso = subprocess.run(
            [sys.executable, "-c", FOLDER_DIALOG_SCRIPT],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise ResolveError("El selector de carpetas tardó demasiado.") from exc
    if proceso.returncode != 0:
        raise ResolveError("No se pudo abrir el selector de carpetas del sistema.")
    return proceso.stdout.strip()


class Job:
    """Estado compartido de una descarga en curso."""

    def __init__(self, total: int) -> None:
        self.id = f"{int(time.time() * 1000)}"
        self.total = total
        self.completados = 0
        self.fallidos = 0
        self.estado = "en_curso"
        self.indice = 0
        self.enlace = ""
        self.archivo = ""
        self.descargado = 0
        self.total_bytes = 0
        self.inicio = time.monotonic()
        self.cancelar = threading.Event()
        self._log: list[str] = []
        self._resultados: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def anadir_log(self, mensaje: str) -> None:
        with self._lock:
            self._log.append(mensaje)
            if len(self._log) > MAX_LOG_LINES:
                del self._log[: len(self._log) - MAX_LOG_LINES]

    def anadir_resultado(self, resultado: dict[str, Any]) -> None:
        with self._lock:
            self._resultados.append(resultado)

    def instantanea(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "estado": self.estado,
                "total": self.total,
                "completados": self.completados,
                "fallidos": self.fallidos,
                "indice": self.indice,
                "enlace": self.enlace,
                "archivo": self.archivo,
                "descargado": self.descargado,
                "total_bytes": self.total_bytes,
                "transcurrido": round(time.monotonic() - self.inicio, 1),
                "log": list(self._log),
                "resultados": list(self._resultados),
            }


class WebResolver(LinkResolver):
    """Resolver que envía su traza al log del trabajo en vez de a stderr."""

    def __init__(self, job: Job, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.job = job

    def _log(self, mensaje: str) -> None:
        self.job.anadir_log(f"{self.prefix}{mensaje}")


TRABAJOS: dict[str, Job] = {}
TRABAJOS_LOCK = threading.Lock()


def ejecutar_trabajo(job: Job, enlaces: list[str], carpeta: Path, force: bool) -> None:
    cache = ResolutionCache(CACHE_FILE, ttl=DEFAULT_CACHE_TTL, enabled=True)
    carpeta.mkdir(parents=True, exist_ok=True)
    log_lock = threading.Lock()

    for indice, url in enumerate(enlaces, 1):
        if job.cancelar.is_set():
            break
        job.indice = indice
        job.enlace = redact_url(url)
        job.archivo = ""
        job.descargado = 0
        job.total_bytes = 0
        job.anadir_log(f"[{indice}/{len(enlaces)}] {redact_url(url)}")
        inicio = time.monotonic()
        resolver = WebResolver(
            job,
            verbose=True,
            cache=cache,
            log_lock=log_lock,
            prefix=f"[{indice}/{len(enlaces)}] ",
        )

        def on_progress(descargado: int, total: int) -> None:
            if job.cancelar.is_set():
                raise ResolveError("Descarga cancelada por el usuario.")
            job.descargado = descargado
            job.total_bytes = total

        try:
            resultado = resolver.resolve(url)
            job.archivo = resultado.filename
            destino = resolver.download(
                resultado,
                str(carpeta),
                force=force,
                as_dir=True,
                progress=on_progress,
            )
            job.completados += 1
            job.anadir_log(f"    guardado {destino.name}")
            job.anadir_resultado(
                {
                    "indice": indice,
                    "enlace": redact_url(url),
                    "archivo": destino.name,
                    "estado": "ok",
                    "error": "",
                    "transcurrido": round(time.monotonic() - inicio, 1),
                }
            )
        except Exception as exc:
            job.fallidos += 1
            job.anadir_log(f"    error: {exc}")
            job.anadir_resultado(
                {
                    "indice": indice,
                    "enlace": redact_url(url),
                    "archivo": "",
                    "estado": "error",
                    "error": str(exc),
                    "transcurrido": round(time.monotonic() - inicio, 1),
                }
            )

    if job.cancelar.is_set():
        job.estado = "cancelado"
    elif job.fallidos:
        job.estado = "terminado_con_errores"
    else:
        job.estado = "terminado"
    job.anadir_log(
        f"Fin: {job.completados}/{job.total} descargados, "
        f"{job.fallidos} con error."
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "LinksWebUI/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(format, *args)

    def _responder(self, codigo: int, cuerpo: bytes, tipo: str) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _json(self, datos: dict[str, Any], codigo: int = 200) -> None:
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self._responder(codigo, cuerpo, "application/json; charset=utf-8")

    def _leer_cuerpo(self) -> dict[str, Any]:
        longitud = int(self.headers.get("Content-Length") or 0)
        if longitud <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(longitud).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ResolveError(f"Cuerpo JSON no válido: {exc}") from exc

    def do_GET(self) -> None:  # noqa: N802
        ruta = self.path.split("?", 1)[0]
        if ruta in ("/", "/index.html"):
            try:
                cuerpo = HTML_FILE.read_bytes()
            except OSError:
                self._responder(500, b"Falta index.html", "text/plain; charset=utf-8")
                return
            self._responder(200, cuerpo, "text/html; charset=utf-8")
            return
        if ruta == "/api/estado":
            job = self._trabajo_de_query()
            if job is None:
                self._json({"error": "Trabajo no encontrado"}, 404)
                return
            self._json(job.instantanea())
            return
        self._json({"error": "Ruta no encontrada"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        ruta = self.path.split("?", 1)[0]
        try:
            datos = self._leer_cuerpo()
        except ResolveError as exc:
            self._json({"error": str(exc)}, 400)
            return

        if ruta == "/api/carpeta":
            try:
                self._json({"carpeta": elegir_carpeta()})
            except ResolveError as exc:
                self._json({"error": str(exc)}, 500)
            return

        if ruta == "/api/descargar":
            self._descargar(datos)
            return

        if ruta == "/api/cancelar":
            job = TRABAJOS.get(str(datos.get("id") or ""))
            if job is None:
                self._json({"error": "Trabajo no encontrado"}, 404)
                return
            job.cancelar.set()
            self._json({"ok": True})
            return

        self._json({"error": "Ruta no encontrada"}, 404)

    def _trabajo_de_query(self) -> Job | None:
        from urllib.parse import parse_qs, urlsplit

        parametros = parse_qs(urlsplit(self.path).query)
        return TRABAJOS.get((parametros.get("id") or [""])[0])

    def _descargar(self, datos: dict[str, Any]) -> None:
        try:
            enlaces = parse_links(str(datos.get("enlaces") or ""))
        except ResolveError as exc:
            self._json({"error": str(exc)}, 400)
            return

        carpeta_texto = str(datos.get("carpeta") or "").strip()
        if not carpeta_texto:
            self._json({"error": "Selecciona una carpeta de destino."}, 400)
            return
        carpeta = Path(carpeta_texto).expanduser()
        try:
            carpeta.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._json({"error": f"No se puede usar esa carpeta: {exc}"}, 400)
            return
        if not carpeta.is_dir():
            self._json({"error": "La ruta de destino no es una carpeta."}, 400)
            return

        job = Job(len(enlaces))
        with TRABAJOS_LOCK:
            TRABAJOS[job.id] = job
        hilo = threading.Thread(
            target=ejecutar_trabajo,
            args=(job, enlaces, carpeta, bool(datos.get("force"))),
            daemon=True,
        )
        hilo.start()
        self._json({"id": job.id, "total": len(enlaces)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interfaz web local para descargar enlaces resueltos."
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Interfaz de escucha (por defecto 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Puerto (por defecto 8765)"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="No abrir el navegador al arrancar"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Registrar cada petición HTTP"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not HTML_FILE.is_file():
        print(f"No se encuentra {HTML_FILE}", file=sys.stderr)
        return 1

    servidor = ThreadingHTTPServer((args.host, args.port), Handler)
    servidor.verbose = args.verbose  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}/"
    print(f"Interfaz de descargas disponible en {url}")
    print("Pulsa Ctrl+C para detenerla.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenida.")
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
