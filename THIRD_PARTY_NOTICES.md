# Third-Party Notices

Plaud Note Manager Community includes third-party software. This file is a compact inventory, not a replacement for the license texts shipped with each component.

## Native application

- GRDB.swift — MIT License. The full text is bundled as `GRDB_LICENSE` in the application resources.

## Embedded runtime

- CPython — Python Software Foundation License. The full text is bundled as `PYTHON_LICENSE`.
- httpx and httpcore — BSD-3-Clause
- keyring — MIT
- pydantic, pydantic-core, annotated-types, typing-inspection — MIT or their packaged license terms
- Typer, Rich, AnyIO, h11, more-itertools, jaraco.context, jaraco.functools, zipp — MIT
- python-dotenv, idna, Click — BSD-3-Clause
- certifi — MPL-2.0
- Pygments — BSD-2-Clause
- Shellingham — ISC
- importlib-metadata — Apache-2.0
- typing-extensions — PSF-2.0
- markdown-it-py, mdurl, jaraco.classes, backports.tarfile and other transitive packages — their packaged license terms

Python package installers preserve available license files inside each `.dist-info/licenses/` directory in the embedded runtime. Run the release audit after dependency updates and review the resulting inventory before redistribution.

Plaud and related names are trademarks of their respective owners. This project is unofficial and is not endorsed by Plaud.
