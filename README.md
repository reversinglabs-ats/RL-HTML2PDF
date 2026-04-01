# RL-HTML2PDF

Render a JS-driven HTML report to PDF using Playwright and a local HTTP server.

## Disclaimer of Warranty

This application is provided "as is" and "as available" without any warranties of any kind, either express or implied.

Reversing Labs make no representations or warranties of any kind, including but not limited to:

- The accuracy, completeness, or timeliness of the information submitted or received via this application;
- The functionality, availability, or performance of the application;
- The security, integrity, or confidentiality of submitted files or user data; or
- The fitness of this application for any particular purpose.

Use of this application is at your own risk. By using this application, you acknowledge that any data submitted to third-party services (e.g., ReversingLabs Spectra Analyze) may be subject to their own terms and conditions.

In no event shall the developer be liable for any direct, indirect, incidental, special, exemplary, or consequential damages arising out of or in any way connected with the use or misuse of this application.

## Requirements

- Python 3.8+
- Playwright for Python
- Google Chrome installed (used via Playwright channel)

Install dependencies:

```bash
pip install .
```

For development (includes ruff, mypy, pytest, pre-commit):

```bash
pip install ".[dev]"
pre-commit install
```

## Usage

Basic:

```bash
python -u RL-HTML2PDF.py
```

With custom input/output:

```bash
python -u RL-HTML2PDF.py -i sdlc.html -o output.pdf
```

## Arguments

- `-i, --input` Input HTML file (default: `sdlc.html`)
- `-o, --output` Output PDF file (default: `output.pdf`)

## Notes

- The script starts a local HTTP server in the HTML file's directory so relative
  assets (like `__deps`) load correctly.
- If Chrome is not available, install it or update the script to use another
  installed browser channel (e.g. `msedge`).
- When `__deps/data.js` exceeds 300 MB, the script automatically splits it into
  smaller binary chunks (`reportData.bin`, `checksData.bin`, `diffData.bin`) and
  serves a lightweight stub JS that loads them via XHR. This avoids Chrome HTTP
  failures when trying to load very large files. The chunks are extracted to a
  temporary directory and cleaned up after each run.

## Design Decisions

### Why Playwright?

The report is a React app — it needs a real browser to run JavaScript and render
the page before we can export it. Browsers block in-page scripts from silently
saving files to disk (`Page.printToPDF` is a DevTools API, not available to
JavaScript). So we need an external tool to drive Chrome.

Playwright is that tool. It's Python-native (no Node.js dependency), installs
with one `pip install`, and gives us full control over Chrome's DevTools Protocol
from outside the browser.

### Why chunking?

A large report's `data.js` encodes binary data as a JavaScript array literal:

```javascript
const reportData = [72,101,108,108,111, /* ...millions of numbers... */];
```

When this file exceeds ~300 MB, Chrome chokes loading it as a `<script>` tag —
it has to download the entire file, parse every number as JavaScript syntax,
build an AST, then execute it. That's several GB of RAM just for parsing.

The fix: extract the raw bytes into `.bin` files and load them via
`XMLHttpRequest` with `responseType='arraybuffer'`. The browser reads binary
straight into a `Uint8Array` — no parsing, no AST, just a memory copy. A ~2 KB
stub JS replaces the original `data.js` and fetches the `.bin` files on demand.

|               | Before                                  | After                            |
| ------------- | --------------------------------------- | -------------------------------- |
| **Transfer**  | One 300 MB+ JS file                     | ~2 KB stub + separate bin files  |
| **Browser**   | Parse millions of JS number literals    | Copy binary into memory          |
| **Memory**    | Source + AST + runtime = several GB     | Raw bytes only                   |
