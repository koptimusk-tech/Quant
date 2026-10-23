// Desktop shell for Quant Anomaly Lab.
//
// Starts the local Python API as a child process (the bundled `qal-server` sidecar in installers, or
// `python -m uvicorn` in development), waits for /api/v1/health and
// then points the window at it. The API serves the built web UI, so the desktop app and the
// browser show the same screens (scanner, tests, research lab, data health, schedules).
// No credentials pass through this shell; the backend reads them from its own environment.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, Url};

const DEFAULT_PORT: u16 = 8765;

struct Backend(Mutex<Option<Child>>);

fn port() -> u16 {
    std::env::var("QAL_DESKTOP_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(DEFAULT_PORT)
}

fn healthy(port: u16) -> bool {
    let Ok(mut s) = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(500)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
    let req = format!("GET /api/v1/health HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n");
    if s.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = String::new();
    let _ = s.read_to_string(&mut buf);
    buf.starts_with("HTTP/1.1 200") || buf.starts_with("HTTP/1.0 200")
}

fn python() -> String {
    std::env::var("QAL_PYTHON").unwrap_or_else(|_| if cfg!(windows) { "python".into() } else { "python3".into() })
}

/// The frozen backend shipped by installers (desktop/scripts/build_sidecar.py), if present.
fn sidecar(resources: &Path) -> Option<PathBuf> {
    let exe = if cfg!(windows) { "qal-server.exe" } else { "qal-server" };
    let path = resources.join("qal-server").join(exe);
    path.is_file().then_some(path)
}

fn spawn_backend(app: &tauri::AppHandle, port: u16) -> std::io::Result<Child> {
    let resources = app.path().resource_dir().unwrap_or_else(|_| PathBuf::from("."));
    let web_dir = std::env::var("QAL_FRONTEND_DIST").map(PathBuf::from).unwrap_or_else(|_| resources.join("web"));
    let data_dir = app.path().app_data_dir().unwrap_or_else(|_| PathBuf::from("./var"));
    std::fs::create_dir_all(&data_dir)?;
    let db = data_dir.join("qal.db");
    // QAL_PYTHON forces the interpreter path even when a sidecar is bundled (useful to debug an install).
    let mut cmd = match sidecar(&resources).filter(|_| std::env::var("QAL_PYTHON").is_err()) {
        Some(exe) => {
            let mut c = Command::new(&exe);
            c.current_dir(&data_dir).args(["--host", "127.0.0.1", "--port", &port.to_string()]);
            c
        }
        None => {
            // Development: QAL_BACKEND_DIR points at the repository's backend/ folder.
            let backend_dir =
                std::env::var("QAL_BACKEND_DIR").map(PathBuf::from).unwrap_or_else(|_| resources.join("backend"));
            let mut c = Command::new(python());
            c.current_dir(&backend_dir)
                .args(["-m", "uvicorn", "qal.api.main:app", "--host", "127.0.0.1", "--port", &port.to_string()])
                .env("PYTHONPATH", &backend_dir);
            c
        }
    };
    cmd.env("QAL_EXIT_WITH_PARENT", "1")
        .env("QAL_FRONTEND_DIST", &web_dir)
        .env("QAL_DATA_DIR", data_dir.join("data"))
        .env("QAL_DATABASE_URL", format!("sqlite:///{}", db.display()).replace('\\', "/"));
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console window behind the app
    }
    cmd.spawn()
}

fn main() {
    let app = tauri::Builder::default()
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            let port = port();
            if !healthy(port) {
                match spawn_backend(&handle, port) {
                    Ok(child) => *app.state::<Backend>().0.lock().unwrap() = Some(child),
                    Err(e) => eprintln!("could not start the local backend: {e}"),
                }
            }
            std::thread::spawn(move || {
                let start = Instant::now();
                while !healthy(port) {
                    if start.elapsed() > Duration::from_secs(90) {
                        if let Some(w) = handle.get_webview_window("main") {
                            let _ = w.eval(
                                "document.getElementById('msg').textContent = 'No se pudo arrancar la API local. \
                                 Reinstala la aplicación o, en desarrollo, revisa QAL_PYTHON y QAL_BACKEND_DIR.'",
                            );
                        }
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(400));
                }
                if let Some(w) = handle.get_webview_window("main") {
                    let _ = w.navigate(Url::parse(&format!("http://127.0.0.1:{port}/tests")).unwrap());
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Tauri application");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(mut child) = handle.state::<Backend>().0.lock().unwrap().take() {
                let _ = child.kill();
            }
        }
    });
}
