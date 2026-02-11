from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import threading
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError as exc:
    raise SystemExit("Playwright is required. Install with: pip install .") from exc

logger = logging.getLogger("rl-html2pdf")

_LARGE_DATA_JS_THRESHOLD = 300_000_000  # 300 MB


# ---------------------------------------------------------------------------
# Large data.js chunking (ported from analyst-workbench report_store.py)
# ---------------------------------------------------------------------------


def _extract_data_array(data_js_path: Path, marker: str, out_path: Path) -> None:
    """Stream data.js in 1 MB chunks, extracting a named byte-array to a .bin file."""
    digit_buffer = ""
    started = False
    finished = False
    pending = bytearray()

    with out_path.open("wb") as out_file:
        with data_js_path.open("r", encoding="utf-8", errors="ignore") as handle:
            search_tail = ""
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                data = search_tail + chunk
                if not started:
                    marker_idx = data.find(marker)
                    if marker_idx == -1:
                        search_tail = data[-len(marker) :]
                        continue
                    bracket_idx = data.find("[", marker_idx)
                    if bracket_idx == -1:
                        search_tail = data[marker_idx:]
                        continue
                    started = True
                    data = data[bracket_idx + 1 :]

                for char in data:
                    if char.isdigit():
                        digit_buffer += char
                        continue
                    if digit_buffer:
                        value = int(digit_buffer)
                        if value < 0 or value > 255:
                            raise RuntimeError(f"Invalid byte value for {marker}: {value}")
                        pending.append(value)
                        if len(pending) >= 1024 * 1024:
                            out_file.write(pending)
                            pending.clear()
                        digit_buffer = ""
                    if char == "]":
                        finished = True
                        break
                if finished:
                    break
                search_tail = ""

            if digit_buffer:
                value = int(digit_buffer)
                if value < 0 or value > 255:
                    raise RuntimeError(f"Invalid byte value for {marker}: {value}")
                pending.append(value)
            if pending:
                out_file.write(pending)

    if not started or not finished:
        raise RuntimeError(f"Failed to extract {marker} array from data.js")


def _extract_cli_context(data_js_path: Path) -> dict:
    """Extract the cliContext JSON object from the tail of data.js."""
    size = data_js_path.stat().st_size
    tail_size = min(size, 500_000)
    with data_js_path.open("rb") as handle:
        if size > tail_size:
            handle.seek(-tail_size, 2)
        tail = handle.read().decode("utf-8", errors="ignore")

    anchor = tail.rfind("cliContext:")
    if anchor == -1:
        raise RuntimeError("cliContext not found in data.js")
    start = tail.find("{", anchor)
    if start == -1:
        raise RuntimeError("cliContext JSON start not found in data.js")

    depth = 0
    in_string = False
    escape = False
    end = None
    for idx in range(start, len(tail)):
        ch = tail[idx]
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break

    if end is None:
        raise RuntimeError("cliContext JSON end not found in data.js")
    return json.loads(tail[start:end])


def _ensure_data_assets(data_js: Path, cache_dir: Path) -> dict[str, Path]:
    """Extract binary arrays and cliContext from data.js into *cache_dir*."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    report_bin = cache_dir / "reportData.bin"
    checks_bin = cache_dir / "checksData.bin"
    diff_bin = cache_dir / "diffData.bin"
    cli_context_path = cache_dir / "cliContext.json"

    logger.info("Extracting reportData from %s ...", data_js.name)
    _extract_data_array(data_js, "reportData", report_bin)

    logger.info("Extracting checksData from %s ...", data_js.name)
    _extract_data_array(data_js, "checksData", checks_bin)

    try:
        logger.info("Extracting diffData from %s ...", data_js.name)
        _extract_data_array(data_js, "diffData", diff_bin)
    except RuntimeError:
        diff_bin.write_bytes(b"")

    logger.info("Extracting cliContext from %s ...", data_js.name)
    cli_context = _extract_cli_context(data_js)
    cli_context_path.write_text(json.dumps(cli_context), encoding="utf-8")

    return {
        "report": report_bin,
        "checks": checks_bin,
        "diff": diff_bin,
        "cli_context": cli_context_path,
    }


def _maybe_patch_data_js(
    data_js: Path, cache_dir: Path, max_bytes: int = _LARGE_DATA_JS_THRESHOLD
) -> Path | None:
    """If *data_js* exceeds *max_bytes*, create a stub that loads .bin files via XHR.

    Returns the stub path if patched, or ``None`` if no patching is needed.
    """
    if data_js.stat().st_size <= max_bytes:
        return None

    assets = _ensure_data_assets(data_js, cache_dir)
    cli_context = json.loads(assets["cli_context"].read_text(encoding="utf-8"))

    stub_path = cache_dir / "data.js"
    cli_context_json = json.dumps(cli_context, separators=(",", ":"))
    stub = (
        "/*rl-html2pdf-stub-v1*/"
        '"use strict";'
        "(globalThis.webpackChunkportal_frontend="
        "globalThis.webpackChunkportal_frontend||[]).push([[543],{2288:(t,a,T)=>{"
        "T.r(a),T.d(a,{default:()=>_});"
        "const u=(n)=>{try{const r=new URL(n,new URL('./',self.location.href));"
        "const x=new XMLHttpRequest();x.open('GET',r.toString(),!1);"
        "try{x.responseType='arraybuffer';}catch(e){}"
        "try{x.overrideMimeType('text/plain; charset=x-user-defined');}catch(e){}"
        "x.send(null);"
        "if(x.status>=200&&x.status<300){"
        "if(x.response&&x.response.byteLength!==undefined){"
        "return new Uint8Array(x.response)}"
        "if(x.responseText){const s=x.responseText;"
        "const a=new Uint8Array(s.length);"
        "for(let i=0;i<s.length;i++){a[i]=s.charCodeAt(i)&255;}return a;}"
        "}"
        "return new Uint8Array(0)}catch(e){return new Uint8Array(0)}};"
        f"const cliContext={cli_context_json};"
        "const reportData=u('__deps/reportData.bin');"
        "const checksData=u('__deps/checksData.bin');"
        "const diffData=u('__deps/diffData.bin');"
        "const _={reportData,checksData,diffData,cliContext};"
        "}}]);"
    )
    stub_path.write_text(stub, encoding="utf-8")
    logger.info(
        "Created stub data.js (%d bytes) replacing original (%d bytes)",
        len(stub),
        data_js.stat().st_size,
    )
    return stub_path


# ---------------------------------------------------------------------------
# Local HTTP server
# ---------------------------------------------------------------------------


@contextmanager
def local_http_server(directory, stub_data_js=None, cache_dir=None):
    """Start a localhost HTTP server for the given directory.

    If *stub_data_js* is provided, requests for ``__deps/data.js`` are served
    from the stub instead of the original.  If *cache_dir* is provided,
    requests for ``__deps/*.bin`` are served from that directory.
    """

    class ReportHandler(SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if stub_data_js and path.endswith("__deps/data.js"):
                self._serve_file(stub_data_js, "application/javascript")
                return
            if cache_dir and path.startswith("/__deps/") and path.endswith(".bin"):
                bin_name = path.split("/")[-1]
                bin_path = Path(cache_dir) / bin_name
                if bin_path.is_file():
                    self._serve_file(bin_path, "application/octet-stream")
                    return
            super().do_GET()

        def _serve_file(self, file_path, content_type):
            """Serve a file with chunked reads to avoid loading it all into memory."""
            try:
                file_path = Path(file_path)
                file_size = file_path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(file_size))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with file_path.open("rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except Exception:
                logger.warning("Failed to serve %s", file_path, exc_info=True)
                self.send_error(500)

    handler = partial(ReportHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        thread.join()


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------


def render_html_to_pdf(html_file_path, output_pdf_path):
    """Render JS-driven HTML to PDF using Playwright."""
    html_path = Path(html_file_path).resolve()
    output_path = Path(output_pdf_path).resolve()

    # --- Large data.js detection ---
    data_js = html_path.parent / "__deps" / "data.js"
    is_large_report = data_js.is_file() and data_js.stat().st_size > _LARGE_DATA_JS_THRESHOLD

    # --- Configuration (timeouts scale up for large reports) ---
    browser_channel = "chrome"
    goto_timeout_ms = 120_000 if is_large_report else 30_000
    networkidle_timeout_ms = 60_000 if is_large_report else 15_000
    ready_selector = "#root > *"
    ready_timeout_ms = 120_000 if is_large_report else 60_000
    min_root_children = 1
    loading_selector = (
        "[role='progressbar'], [aria-busy='true'], .loading, .spinner, .MuiCircularProgress-root"
    )
    loading_timeout_ms = 120_000 if is_large_report else 60_000
    render_settle_ms = 5000 if is_large_report else 2000
    landscape = False
    pdf_format = "A4"
    pdf_margin = "0mm"
    pdf_scale = 1.0
    viewport_width = 1280
    viewport_height = 900

    with tempfile.TemporaryDirectory(prefix="rl-html2pdf-") as tmp:
        tmp_dir = Path(tmp)

        # --- Patch large data.js if needed ---
        stub_data_js = None
        cache_dir = None
        if is_large_report:
            logger.info(
                "Large data.js detected (%d MB); splitting into chunks",
                data_js.stat().st_size // (1024 * 1024),
            )
            cache_dir = tmp_dir
            stub_data_js = _maybe_patch_data_js(data_js, cache_dir)

        with local_http_server(
            html_path.parent, stub_data_js=stub_data_js, cache_dir=cache_dir
        ) as port:
            url = f"http://127.0.0.1:{port}/{html_path.name}"
            with sync_playwright() as p:
                browser = p.chromium.launch(channel=browser_channel)
                try:
                    page = browser.new_page()
                    page.set_viewport_size({"width": viewport_width, "height": viewport_height})

                    page.goto(url, wait_until="load", timeout=goto_timeout_ms)
                    try:
                        page.wait_for_load_state("networkidle", timeout=networkidle_timeout_ms)
                    except Exception:
                        logger.warning(
                            "Network did not reach idle within %d ms; proceeding",
                            networkidle_timeout_ms,
                        )

                    try:
                        page.wait_for_selector(ready_selector, timeout=ready_timeout_ms)
                    except Exception:
                        logger.warning(
                            "Ready selector '%s' not found within %d ms; proceeding",
                            ready_selector,
                            ready_timeout_ms,
                        )

                    try:
                        page.wait_for_function(
                            """arg => {
                                const root = document.querySelector('#root');
                                const hasChildren = root && root.children.length >= arg.minChildren;
                                const loading = document.querySelector(arg.loadingSelector);
                                return document.readyState === 'complete' && hasChildren && !loading;
                            }""",
                            arg={
                                "minChildren": min_root_children,
                                "loadingSelector": loading_selector,
                            },
                            timeout=loading_timeout_ms,
                        )
                    except Exception:
                        logger.warning(
                            "Page did not finish loading within %d ms; proceeding",
                            loading_timeout_ms,
                        )

                    if render_settle_ms:
                        page.wait_for_timeout(render_settle_ms)

                    page.emulate_media(media="print")
                    page_width_in = {
                        "A4": 8.27,
                        "Letter": 8.5,
                        "Legal": 8.5,
                        "Tabloid": 11.0,
                        "Ledger": 17.0,
                    }.get(str(pdf_format))
                    effective_scale = pdf_scale
                    if page_width_in:
                        fit_scale = (page_width_in * 96) / viewport_width
                        effective_scale = min(pdf_scale, fit_scale)
                    page.pdf(
                        path=str(output_path),
                        print_background=True,
                        landscape=landscape,
                        format=pdf_format,
                        scale=effective_scale,
                        margin={
                            "top": pdf_margin,
                            "right": pdf_margin,
                            "bottom": pdf_margin,
                            "left": pdf_margin,
                        },
                        prefer_css_page_size=True,
                    )
                finally:
                    browser.close()

    logger.info("PDF generated and saved at %s", output_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(description="Render an HTML report to PDF using Playwright.")
    parser.add_argument(
        "-i",
        "--input",
        default="sdlc.html",
        help="Input HTML file (default: sdlc.html)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="output.pdf",
        help="Output PDF file (default: output.pdf)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = base_dir / input_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = base_dir / output_path

    if not input_path.is_file():
        logger.error("Input file not found: %s", input_path)
        sys.exit(1)

    render_html_to_pdf(input_path, output_path)
