# App de escritorio

Ventana nativa (Tauri 2) que arranca el motor local (la API de Python) y muestra la misma interfaz que el
navegador, empezando por la pantalla **Tests**: elegir pruebas (detectores con sus parámetros, pares,
validaciones de hipótesis), ejecutarlas al momento y programarlas (con descarga previa de datos nuevos).

Al cerrar la ventana se detiene la API que lanzó. Si ya hay una API escuchando en el puerto (por defecto 8765),
la app la reutiliza.

## Requisitos

- Python ≥ 3.11 con el backend instalado: `pip install -e "./backend[all]"` (en Windows añade
  `pip install MetaTrader5` para leer del terminal MT5).
- Para compilar: Rust (rustup), Node ≥ 20 y los requisitos de Tauri de tu sistema
  (Windows: WebView2 y Build Tools de Visual Studio; Linux: `libwebkit2gtk-4.1-dev` y relacionados).

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
cd desktop && npm run build      # compila el frontend y genera el instalador (.msi/.exe en Windows, .deb/.AppImage en Linux, .dmg en macOS)
```

El instalador incluye el código del backend y la interfaz compilada, pero **no incluye Python**: la app usa el
intérprete indicado en `QAL_PYTHON` (por defecto `python` en Windows, `python3` en el resto), que debe tener
instaladas las dependencias del backend. Empaquetar un intérprete propio es una mejora pendiente (ROADMAP).

Variables: `QAL_PYTHON`, `QAL_BACKEND_DIR`, `QAL_FRONTEND_DIST`, `QAL_DESKTOP_PORT` y todas las `QAL_*` del backend.
Los datos de la app (SQLite y Parquet) se guardan en el directorio de datos de la aplicación del sistema.
Las credenciales de proveedores se leen del entorno del proceso de la API, nunca pasan por la ventana.
