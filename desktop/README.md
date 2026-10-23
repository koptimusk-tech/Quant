# App de escritorio

Ventana nativa (Tauri 2) que arranca el motor local (la API de Python) y muestra la misma interfaz que el
navegador, empezando por la pantalla **Tests**: elegir pruebas (detectores con sus parámetros, pares,
validaciones de hipótesis), ejecutarlas al momento y programarlas (con descarga previa de datos nuevos).

Al cerrar la ventana se detiene la API que lanzó. Si ya hay una API escuchando en el puerto (por defecto 8765),
la app la reutiliza.

## Descargar e instalar

Los instaladores se generan en GitHub Actions (workflow **Desktop installers**) para Windows (`.msi` y
`-setup.exe`), macOS (`.dmg`) y Linux (`.deb`, `.AppImage`, `.rpm`). Llevan el motor de Python congelado
dentro, así que **no hace falta instalar Python**. El de Windows incluye además la librería de MetaTrader 5, que
lee del terminal MT5 instalado en el mismo equipo.

- Ejecutarlo: en GitHub, pestaña *Actions* → *Desktop installers* → *Run workflow*. Al terminar, los
  instaladores están en *Artifacts* de esa ejecución.
- Publicar una versión: crear un tag `vX.Y.Z`; el workflow adjunta los instaladores a la release.

Los instaladores no están firmados: Windows SmartScreen pedirá "Más información → Ejecutar de todas formas" y
macOS pedirá abrirlo con clic derecho → *Abrir* la primera vez.

## Requisitos para compilar

- Rust (rustup), Node ≥ 20 y los requisitos de Tauri de tu sistema (Windows: WebView2 y Build Tools de Visual
  Studio; Linux: `libwebkit2gtk-4.1-dev` y relacionados).
- Python ≥ 3.11 con el backend instalado: `pip install -e "./backend[all]"` (en Windows añade
  `pip install MetaTrader5`).

## Desarrollo

```bash
cd frontend && npm install && npm run build && cd ..
cd desktop && npm install
QAL_PYTHON=../.venv/bin/python QAL_BACKEND_DIR=../backend QAL_FRONTEND_DIST=../frontend/dist npx tauri dev
```

En Windows (PowerShell):

```powershell
$env:QAL_PYTHON="..\\.venv\\Scripts\\python.exe"; $env:QAL_BACKEND_DIR="..\\backend"; $env:QAL_FRONTEND_DIST="..\\frontend\\dist"
npx tauri dev
```

## Instalador

```bash
pip install -e "./backend[postgres,yahoo,crypto]" pyinstaller   # + [metatrader] en Windows
python desktop/scripts/build_sidecar.py                          # congela el backend en desktop/sidecar/qal-server
cd frontend && npm run build && cd ../desktop
npx tauri build --config src-tauri/tauri.sidecar.conf.json
```

`tauri.sidecar.conf.json` sustituye el código fuente del backend por el ejecutable congelado `qal-server`.
PyInstaller no compila para otros sistemas: cada instalador se construye en su propio sistema operativo (por eso
lo hace GitHub Actions). Sin esa configuración (`npm run build`), el instalador lleva el código fuente y usa el
Python indicado en `QAL_PYTHON`.

Al arrancar, la app usa `qal-server` si viene incluido y, si no, `QAL_PYTHON -m uvicorn`. Definir `QAL_PYTHON`
fuerza el modo Python aunque haya un `qal-server` (útil para depurar una instalación).

Variables: `QAL_PYTHON`, `QAL_BACKEND_DIR`, `QAL_FRONTEND_DIST`, `QAL_DESKTOP_PORT` y todas las `QAL_*` del backend.
Los datos de la app (SQLite y Parquet) se guardan en el directorio de datos de la aplicación del sistema.
Las credenciales de proveedores se leen del entorno del proceso de la API, nunca pasan por la ventana.
