#!/usr/bin/env python3
"""Resuelve cadenas de redirección con esperas y publicidad, y descarga el archivo final.

Ejemplos:
    python3 resolve_links.py "https://web-api-13.vercel.app/?url=https://softurl.in/XXXX"
    python3 resolve_links.py "https://softurl.in/XXXX" --info
    python3 resolve_links.py "https://softurl.in/XXXX" -o mis_descargas
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import posixpath
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import (
    parse_qs,
    parse_qsl,
    quote,
    unquote,
    unquote_plus,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import requests
from bs4 import BeautifulSoup

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_WAIT = 90.0
DEFAULT_MAX_HOPS = 25
DEFAULT_WAIT_BUFFER = 0.4
DEFAULT_CACHE_TTL = 7 * 24 * 3600
BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / ".resolve_cache.json"
DOWNLOAD_DIR = Path("downloads")

_DEST_LOCK = threading.Lock()
_RESERVED_PATHS: set[Path] = set()


class ResolveError(RuntimeError):
    """Error controlado durante la resolución o la descarga."""


@dataclass
class Action:
    kind: str
    url: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    filename: str = ""


@dataclass
class Resolution:
    final_url: str
    filename: str
    referer: str
    hops: int
    page_url: str | None = None


@dataclass
class JobResult:
    index: int
    start_url: str
    resolution: Resolution | None = None
    destination: Path | None = None
    skipped: bool = False
    error: Exception | None = None
    elapsed: float = 0.0


def extract_wrapper_target(url: str) -> str | None:
    """Extrae el parámetro ?url= de wrappers tipo web-api-*.vercel.app."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.query:
        return None
    if "vercel.app" not in parts.netloc and "web-api" not in parts.netloc:
        return None
    values = parse_qs(parts.query).get("url")
    if not values:
        return None
    target = values[0].strip()
    if not target.startswith(("http://", "https://")):
        return None
    return target


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def read_urls_file(path: str | Path) -> list[str]:
    """Lee URLs de un archivo de texto, una por línea (o separadas por espacios)."""
    file_path = Path(path)
    urls: list[str] = []
    with file_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for token in line.split():
                if not token.startswith(("http://", "https://")):
                    raise ResolveError(
                        f"URL inválida en {file_path}:{line_number}: {token!r}"
                    )
                urls.append(token)
    if not urls:
        raise ResolveError(f"No se encontraron URLs en {file_path}.")
    return urls


def collect_urls(positional: list[str], files: list[str] | None) -> list[str]:
    """Combina URLs pasadas por argumento con las de uno o varios archivos."""
    urls: list[str] = []
    for item in positional:
        candidate = Path(item).expanduser()
        if candidate.is_file():
            urls.extend(read_urls_file(candidate))
        elif item.startswith(("http://", "https://")):
            urls.append(item)
        else:
            raise ResolveError(f"No es una URL ni un archivo existente: {item}")
    for file_name in files or []:
        urls.extend(read_urls_file(file_name))
    if not urls:
        raise ResolveError("No se indicó ninguna URL.")
    return urls


def cache_key(url: str) -> str:
    """Normaliza la clave de caché: wrapper y destino directo comparten entrada."""
    return (extract_wrapper_target(url) or url).strip()


def base_destination(output: str | None, filename: str, as_dir: bool) -> Path:
    """Ruta de destino sin resolver nombres duplicados."""
    if output is None:
        return DOWNLOAD_DIR / filename
    base = Path(output).expanduser()
    if as_dir or str(output).endswith(("/", os.sep)) or base.is_dir():
        return base / filename
    return base


class ResolutionCache:
    """Caché local y thread-safe de páginas finales estables (sin tokens)."""

    def __init__(
        self,
        path: str | Path = CACHE_FILE,
        *,
        ttl: float = DEFAULT_CACHE_TTL,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path)
        self.ttl = ttl
        self.enabled = enabled
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.enabled:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict)
        }

    def _save(self) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        except OSError:
            pass

    def get(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        with self._lock:
            entry = self._data.get(key)
            if not isinstance(entry, dict):
                return None
            try:
                saved_at = float(entry.get("saved_at", 0))
            except (TypeError, ValueError):
                saved_at = 0
            if time.time() - saved_at > self.ttl:
                self._data.pop(key, None)
                self._save()
                return None
            return dict(entry)

    def put(self, key: str, page_url: str, filename: str) -> None:
        if not self.enabled or not page_url:
            return
        with self._lock:
            self._data[key] = {
                "page_url": page_url,
                "filename": filename,
                "saved_at": time.time(),
            }
            self._save()

    def remove(self, key: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._data.pop(key, None) is not None:
                self._save()


def redact_url(url: str, max_value: int = 24) -> str:
    """Acorta valores sensibles para los logs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    sensitive = re.compile(r"(token|key|sig|hash|auth|session|csrf|redirect|url)", re.I)
    pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if sensitive.search(key) or len(value) > max_value:
            if len(value) > 12:
                value = f"{value[:6]}...{value[-4:]}"
            else:
                value = "..."
        pairs.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = (name or "").strip().strip('"').strip()
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return "download.bin"
    if len(name) > max_len:
        stem, suffix = os.path.splitext(name)
        keep = max(1, max_len - len(suffix))
        name = stem[:keep] + suffix
    return name


def filename_from_url(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    base = posixpath.basename(path.rstrip("/"))
    if not base:
        return None
    return unquote_plus(base)


def filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)?''([^;]+)", value, re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename\s*=\s*"([^"]+)"', value, re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    if match:
        return match.group(1).strip().strip('"')
    return None


def parse_counter(html: str) -> float:
    match = re.search(r'"counter_value"\s*:\s*"([^"]+)"', html)
    if match:
        try:
            return max(0.0, float(match.group(1)))
        except ValueError:
            pass
    match = re.search(r'id=["\']custom-timer["\'][^>]*>\s*([\d.]+)', html, re.I)
    if match:
        try:
            return max(0.0, float(match.group(1)))
        except ValueError:
            pass
    return 1.0


def decode_safelink(value: str) -> dict:
    text = unquote(value.strip())
    text += "=" * (-len(text) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(text)
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    raise ResolveError("No se pudo decodificar el payload newwpsafelink.")


def form_fields(form) -> dict[str, str]:
    fields: dict[str, str] = {}
    for element in form.find_all(["input", "textarea", "select"]):
        name = element.get("name")
        if not name:
            continue
        input_type = (element.get("type") or "").lower()
        if input_type in {"submit", "button", "image", "reset", "file"}:
            continue
        if element.name == "select":
            option = element.find("option", selected=True) or element.find("option")
            value = ""
            if option is not None:
                value = option.get("value", option.get_text(strip=True))
        elif element.name == "textarea":
            value = element.get_text()
        else:
            value = element.get("value", "")
        fields[name] = value
    return fields


def detect_action(html: str, base_url: str) -> Action | None:
    soup = BeautifulSoup(html, "html.parser")

    download_button = soup.find("a", id="downloadButton")
    if download_button is not None and download_button.get("href"):
        href = urljoin(base_url, download_button["href"].strip())
        return Action("mediafire", url=href, filename=filename_from_url(href) or "")

    go_form = soup.find("form", id="go-link")
    if go_form is None:
        ad_input = soup.find("input", {"name": "ad_form_data"})
        if ad_input is not None:
            go_form = ad_input.find_parent("form")
    if go_form is not None:
        fields = form_fields(go_form)
        if "ad_form_data" in fields:
            action_url = urljoin(base_url, go_form.get("action") or base_url)
            return Action("xhr_post", url=action_url, fields=fields, delay=parse_counter(html))

    safelink_input = soup.find("input", {"name": "newwpsafelink"})
    if safelink_input is not None and safelink_input.get("value"):
        payload = decode_safelink(safelink_input["value"])
        try:
            delay = max(0.0, float(payload.get("delay", 25)))
        except (TypeError, ValueError):
            delay = 25.0
        linkr = payload.get("linkr")
        if linkr:
            return Action("wait_get", url=urljoin(base_url, str(linkr)), delay=delay)
        safelink_form = safelink_input.find_parent("form")
        if safelink_form is not None:
            return Action(
                "wait_post_form",
                url=urljoin(base_url, safelink_form.get("action") or base_url),
                fields=form_fields(safelink_form),
                delay=delay,
            )

    landing = soup.find("form", id="landing")
    if landing is None:
        go_input = soup.find("input", {"name": "go"})
        if go_input is not None:
            landing = go_input.find_parent("form")
    if landing is not None:
        fields = form_fields(landing)
        if "go" in fields:
            return Action(
                "post_form",
                url=urljoin(base_url, landing.get("action") or base_url),
                fields=fields,
            )

    op_input = soup.find("input", {"name": "op"})
    if op_input is not None and "download" in (op_input.get("value") or "").lower():
        download_form = op_input.find_parent("form")
        if download_form is not None:
            return Action(
                "post_form",
                url=urljoin(base_url, download_form.get("action") or base_url),
                fields=form_fields(download_form),
            )

    meta = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
    if meta is not None:
        content = meta.get("content", "")
        match = re.search(r"url\s*=\s*([^;]+)", content, re.I)
        if match:
            target = match.group(1).strip().strip("'\"")
            return Action("goto", url=urljoin(base_url, target))

    for pattern in (
        r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
        r'window\.location\.replace\(\s*["\']([^"\']+)["\']\s*\)',
        r'location\.href\s*=\s*["\']([^"\']+)["\']',
    ):
        match = re.search(pattern, html)
        if match:
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "/")):
                return Action("goto", url=urljoin(base_url, target))

    download_attr = soup.find("a", attrs={"download": True}, href=True)
    if download_attr is not None:
        return Action("mediafire", url=urljoin(base_url, download_attr["href"].strip()))

    return None


def is_html_response(response: requests.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type or "xhtml" in content_type:
        return True
    if content_type:
        return False
    return True


def is_challenge(status: int, html: str) -> bool:
    if status in (403, 503) and any(
        marker in html for marker in ("Just a moment", "cf-chl", "cf-challenge")
    ):
        return True
    return any(
        marker in html
        for marker in (
            "Just a moment...",
            "Enable JavaScript and cookies to continue",
            "cf-challenge",
        )
    )


def unique_path(path: Path, avoid: set[Path] | None = None) -> Path:
    avoid = avoid or set()

    def available(candidate: Path) -> bool:
        return not candidate.exists() and candidate not in avoid

    if available(path):
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if available(candidate):
            return candidate
    return path.with_name(f"{stem}-{int(time.time())}{suffix}")


class LinkResolver:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_wait: float = DEFAULT_MAX_WAIT,
        max_hops: int = DEFAULT_MAX_HOPS,
        verbose: bool = False,
        full_chain: bool = False,
        wait_buffer: float = DEFAULT_WAIT_BUFFER,
        cache: ResolutionCache | None = None,
        log_lock: threading.Lock | None = None,
        prefix: str = "",
    ) -> None:
        self.timeout = timeout
        self.max_wait = max_wait
        self.max_hops = max_hops
        self.verbose = verbose
        self.full_chain = full_chain
        self.wait_buffer = max(0.0, wait_buffer)
        self.cache = cache
        self.log_lock = log_lock
        self.prefix = prefix
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": DEFAULT_UA,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        self.session.max_redirects = 15

    def _log(self, message: str) -> None:
        line = f"{self.prefix}{message}"
        if self.log_lock is None:
            print(line, file=sys.stderr, flush=True)
        else:
            with self.log_lock:
                print(line, file=sys.stderr, flush=True)

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = True,
    ) -> requests.Response:
        request_headers: dict[str, str] = {}
        if headers:
            request_headers.update(headers)
        if method.upper() == "POST" and "Origin" not in request_headers:
            request_headers["Origin"] = origin_of(url)
        response = self.session.request(
            method,
            url,
            data=data,
            headers=request_headers,
            timeout=self.timeout,
            allow_redirects=True,
            stream=stream,
        )
        return response

    def _wait(self, seconds: float, reason: str) -> None:
        if seconds > self.max_wait:
            raise ResolveError(
                f"La espera requerida ({seconds:.0f}s) supera --max-wait "
                f"({self.max_wait:.0f}s)."
            )
        if seconds > 0:
            self._log(f"    esperando {seconds:.1f}s ({reason})...")
            time.sleep(seconds)

    def _prepare_start(self, url: str) -> str:
        target = extract_wrapper_target(url)
        if target is None:
            return url
        if not self.full_chain:
            self._log(f"Wrapper detectado; usando destino directo: {redact_url(target)}")
            return target
        parts = urlsplit(url)
        api_url = (
            f"{parts.scheme}://{parts.netloc}/api/shorten"
            f"?url={quote(target, safe='')}"
        )
        self._log(f"Modo cadena completa; consultando {redact_url(api_url)}")
        response = self._request("GET", api_url, stream=False)
        try:
            data = response.json()
        except ValueError as exc:
            raise ResolveError("El wrapper no devolvió JSON.") from exc
        finally:
            response.close()
        shortened = data.get("shortenedUrl")
        if not shortened:
            raise ResolveError(f"El wrapper no devolvió shortenedUrl: {data}")
        return str(shortened)

    def resolve(self, start_url: str, *, use_cache: bool = True) -> Resolution:
        key = cache_key(start_url)
        if use_cache and self.cache is not None:
            entry = self.cache.get(key)
            if entry and entry.get("page_url"):
                page_url = str(entry["page_url"])
                self._log(f"    caché encontrada; validando {redact_url(page_url)}")
                try:
                    result = self._resolve_from(page_url)
                except Exception as exc:
                    self._log(f"    caché no válida ({exc}); resolviendo de nuevo")
                    self.cache.remove(key)
                else:
                    if result.page_url is None:
                        result.page_url = page_url
                    self.cache.put(key, result.page_url, result.filename)
                    return result

        result = self._resolve_from(start_url)
        if self.cache is not None and result.page_url:
            self.cache.put(key, result.page_url, result.filename)
        return result

    def _resolve_from(self, start_url: str) -> Resolution:
        url = self._prepare_start(start_url.strip())
        method = "GET"
        data: dict[str, str] | None = None
        referer: str | None = None
        visit_counts: dict[tuple[str, str], int] = {}

        for hop in range(1, self.max_hops + 1):
            key = (method, url)
            count = visit_counts.get(key, 0)
            if count >= 3:
                raise ResolveError(f"Bucle detectado en {redact_url(url)}.")
            visit_counts[key] = count + 1

            headers: dict[str, str] = {}
            if referer:
                headers["Referer"] = referer
            response = self._request(method, url, data=data, headers=headers)
            response_url = response.url
            status = response.status_code

            self._log(
                f"[{hop}] {method} {redact_url(url)} -> "
                f"{status} {redact_url(response_url)}"
            )
            for redirect in response.history:
                self._log(f"    {redirect.status_code} {redact_url(redirect.url)}")
            if self.verbose:
                content_type = response.headers.get("content-type", "?")
                self._log(f"    content-type: {content_type}")

            if status >= 400:
                html = response.text[:1000] if is_html_response(response) else ""
                response.close()
                if is_challenge(status, html):
                    raise ResolveError(
                        "Cloudflare/captcha bloqueó la petición. "
                        "Prueba de nuevo más tarde o desde otra red."
                    )
                raise ResolveError(f"HTTP {status} en {redact_url(response_url)}")

            if not is_html_response(response):
                filename = (
                    filename_from_disposition(
                        response.headers.get("content-disposition")
                    )
                    or filename_from_url(response_url)
                    or "download.bin"
                )
                result = Resolution(
                    final_url=response_url,
                    filename=sanitize_filename(filename),
                    referer=referer or response_url,
                    hops=hop,
                )
                response.close()
                return result

            html = response.text
            action = detect_action(html, base_url=response_url)
            if action is not None and self.verbose:
                self._log(f"    acción detectada: {action.kind}")
            if action is None:
                if "too early" in html.lower() and visit_counts[key] < 3:
                    self._log("    página 'Too Early'; reintentando en 3 s...")
                    response.close()
                    time.sleep(3)
                    method, url, data = "GET", response_url, None
                    continue
                title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
                title = title_match.group(1).strip() if title_match else "sin título"
                response.close()
                raise ResolveError(
                    f"No se reconoce la página en {redact_url(response_url)} "
                    f"(título: {title!r})."
                )

            response.close()
            kind = action.kind
            if kind == "post_form":
                self._log(f"    formulario autoenviado -> {redact_url(action.url)}")
                method, url, data, referer = "POST", action.url, action.fields, response_url
            elif kind == "wait_post_form":
                self._wait(action.delay + self.wait_buffer, "verificación del acortador")
                method, url, data, referer = "POST", action.url, action.fields, response_url
            elif kind == "wait_get":
                self._wait(action.delay + self.wait_buffer, "verificación del acortador")
                method, url, data, referer = "GET", action.url, None, response_url
            elif kind == "goto":
                method, url, data, referer = "GET", action.url, None, response_url
            elif kind == "xhr_post":
                self._wait(action.delay + self.wait_buffer, "contador de AdLinkFly")
                xhr_headers = {
                    "Referer": response_url,
                    "Origin": origin_of(response_url),
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                }
                xhr = self._request(
                    "POST", action.url, data=action.fields, headers=xhr_headers
                )
                try:
                    if xhr.status_code >= 400:
                        raise ResolveError(
                            f"HTTP {xhr.status_code} en {redact_url(action.url)}"
                        )
                    try:
                        payload = xhr.json()
                    except ValueError as exc:
                        snippet = xhr.text[:300]
                        raise ResolveError(
                            f"La respuesta de /links/go no es JSON: {snippet!r}"
                        ) from exc
                finally:
                    xhr.close()
                next_url = payload.get("url")
                if not next_url:
                    message = payload.get("message", "respuesta sin URL")
                    raise ResolveError(f"El servidor no devolvió URL: {message}")
                self._log("    enlace final obtenido del contador")
                method, url, data, referer = "GET", str(next_url), None, response_url
            elif kind == "mediafire":
                self._log("    enlace directo de descarga detectado")
                return Resolution(
                    final_url=action.url,
                    filename=sanitize_filename(
                        action.filename or filename_from_url(action.url) or "download.bin"
                    ),
                    referer=response_url,
                    hops=hop,
                    page_url=response_url,
                )
            else:
                raise ResolveError(f"Acción no soportada: {kind}")

        raise ResolveError(f"Se superó el límite de {self.max_hops} saltos.")

    def download(
        self,
        target: Resolution,
        output: str | None = None,
        *,
        force: bool = False,
        as_dir: bool = True,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Descarga el archivo final.

        Si se pasa ``progress``, se invoca con ``(descargado, total)`` en bytes
        (``total`` puede ser 0 si el servidor no envía ``content-length``).
        """
        headers = {
            "Referer": target.referer or target.final_url,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        destination: Path | None = None
        response = self.session.get(
            target.final_url,
            headers=headers,
            timeout=self.timeout,
            allow_redirects=True,
            stream=True,
        )
        try:
            if response.status_code >= 400:
                raise ResolveError(
                    f"HTTP {response.status_code} al descargar "
                    f"{redact_url(target.final_url)}"
                )
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type:
                snippet = response.text[:300]
                raise ResolveError(
                    "El enlace de descarga devolvió HTML en vez del archivo "
                    f"(posible login o bloqueo): {snippet!r}"
                )

            disposition_name = filename_from_disposition(
                response.headers.get("content-disposition")
            )
            filename = sanitize_filename(
                disposition_name
                or target.filename
                or filename_from_url(response.url)
                or "download.bin"
            )
            destination = self._destination(
                output, filename, as_dir=as_dir, force=force
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            part = Path(str(destination) + ".part")
            total = int(response.headers.get("content-length") or 0)
            downloaded = 0
            last_progress = 0.0
            if progress is not None:
                progress(0, total)

            try:
                with part.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if progress is not None:
                            progress(downloaded, total)
                        if (
                            sys.stderr.isatty()
                            and total
                            and now - last_progress >= 0.5
                        ):
                            last_progress = now
                            percent = min(100, downloaded * 100 // total)
                            print(
                                f"\r    descargando {percent:3d}% "
                                f"({downloaded}/{total} bytes)",
                                end="",
                                file=sys.stderr,
                                flush=True,
                            )
                if sys.stderr.isatty() and total:
                    print(file=sys.stderr)
                if total and downloaded != total:
                    raise ResolveError(
                        f"Descarga incompleta: {downloaded}/{total} bytes."
                    )
                part.replace(destination)
            except BaseException:
                part.unlink(missing_ok=True)
                raise

            self._log(f"    guardado {destination} ({downloaded} bytes)")
            return destination
        finally:
            if destination is not None:
                with _DEST_LOCK:
                    _RESERVED_PATHS.discard(destination)
            response.close()

    def _destination(
        self, output: str | None, filename: str, *, as_dir: bool, force: bool
    ) -> Path:
        with _DEST_LOCK:
            destination = base_destination(output, filename, as_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination in _RESERVED_PATHS:
                destination = unique_path(destination, _RESERVED_PATHS)
            elif destination.exists() and not force:
                destination = unique_path(destination, _RESERVED_PATHS)
            _RESERVED_PATHS.add(destination)
            return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Resuelve cadenas de redirección con esperas/publicidad y descarga "
            "el archivo final."
        )
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="URL(s) de inicio o archivo(s) .txt con una URL por línea",
    )
    parser.add_argument(
        "-f",
        "--file",
        action="append",
        metavar="ARCHIVO",
        help="Archivo con URLs, una por línea. Se puede repetir.",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Solo mostrar el enlace final, sin descargar",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Archivo de destino o carpeta (por defecto ./downloads)",
    )
    parser.add_argument(
        "--full-chain",
        action="store_true",
        help="Seguir el wrapper /api/shorten en vez de extraer el destino directo",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Sobrescribir archivos existentes",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Mostrar detalles adicionales",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout por petición en segundos (por defecto {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=DEFAULT_MAX_WAIT,
        help=f"Espera máxima permitida por temporizador (por defecto {DEFAULT_MAX_WAIT:g})",
    )
    parser.add_argument(
        "--max-hops",
        type=int,
        default=DEFAULT_MAX_HOPS,
        help=f"Máximo de saltos (por defecto {DEFAULT_MAX_HOPS})",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=0,
        metavar="N",
        help="Enlaces resueltos en paralelo (0 = automático: hasta 4)",
    )
    parser.add_argument(
        "--wait-buffer",
        type=float,
        default=DEFAULT_WAIT_BUFFER,
        metavar="SEGUNDOS",
        help=(
            "Margen extra tras cada temporizador del servidor "
            f"(por defecto {DEFAULT_WAIT_BUFFER:g} s)"
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="No leer ni escribir la caché de resoluciones",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignorar la caché y resolver de nuevo (la actualiza)",
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=168.0,
        metavar="HORAS",
        help="Validez de la caché en horas (por defecto 168)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="No descargar si el archivo ya existe con contenido",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        urls = collect_urls(args.urls, args.file)
    except (OSError, ResolveError) as exc:
        print(f"Error leyendo los enlaces: {exc}", file=sys.stderr)
        return 1

    as_dir = True
    if (
        args.output
        and len(urls) == 1
        and not str(args.output).endswith(("/", os.sep))
        and not Path(args.output).is_dir()
    ):
        as_dir = False

    jobs = args.jobs if args.jobs > 0 else (
        1 if len(urls) == 1 else min(4, len(urls))
    )
    jobs = max(1, min(jobs, len(urls)))

    cache = ResolutionCache(
        CACHE_FILE,
        ttl=max(0.0, args.cache_ttl) * 3600,
        enabled=not args.no_cache,
    )
    log_lock = threading.Lock()
    total = len(urls)

    def run_job(index: int, start_url: str) -> JobResult:
        prefix = f"[{index}/{total}] " if total > 1 else ""
        started = time.monotonic()
        resolver = LinkResolver(
            timeout=args.timeout,
            max_wait=args.max_wait,
            max_hops=args.max_hops,
            verbose=args.verbose,
            full_chain=args.full_chain,
            wait_buffer=args.wait_buffer,
            cache=cache,
            log_lock=log_lock,
            prefix=prefix,
        )
        try:
            result = resolver.resolve(start_url, use_cache=not args.refresh)
            if args.info:
                return JobResult(
                    index,
                    start_url,
                    result,
                    elapsed=time.monotonic() - started,
                )
            if args.skip_existing and not args.force:
                candidate = base_destination(args.output, result.filename, as_dir)
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return JobResult(
                        index,
                        start_url,
                        result,
                        candidate,
                        skipped=True,
                        elapsed=time.monotonic() - started,
                    )
            destination = resolver.download(
                result, args.output, force=args.force, as_dir=as_dir
            )
            return JobResult(
                index,
                start_url,
                result,
                destination,
                elapsed=time.monotonic() - started,
            )
        except Exception as exc:
            return JobResult(
                index,
                start_url,
                error=exc,
                elapsed=time.monotonic() - started,
            )

    try:
        if jobs == 1:
            results = [run_job(index, url) for index, url in enumerate(urls, 1)]
        else:
            with ThreadPoolExecutor(max_workers=jobs) as executor:
                futures = [
                    executor.submit(run_job, index, url)
                    for index, url in enumerate(urls, 1)
                ]
                results = [future.result() for future in futures]
    except KeyboardInterrupt:
        print("\nCancelado.", file=sys.stderr)
        return 130

    failures = 0
    for job in results:
        if job.error is not None:
            failures += 1
            print(
                f"Error con {redact_url(job.start_url)}: {job.error}",
                file=sys.stderr,
            )
            continue
        if job.resolution is None:
            failures += 1
            print(
                f"Error con {redact_url(job.start_url)}: sin resultado",
                file=sys.stderr,
            )
            continue
        if args.info:
            print(job.resolution.final_url)
            continue
        if job.skipped:
            print(f"Omitido (ya existe): {job.destination} ({job.elapsed:.1f}s)")
            continue
        size = job.destination.stat().st_size if job.destination else 0
        print(f"URL final: {job.resolution.final_url}")
        print(f"Archivo: {job.resolution.filename}")
        print(f"Descargado: {job.destination} ({size} bytes) ({job.elapsed:.1f}s)")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
