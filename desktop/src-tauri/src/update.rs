use super::{authorized, Resident};
use serde::Serialize;
use std::{
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager, WebviewWindow};
use tauri_plugin_updater::{Update, UpdaterExt};

const DAY: Duration = Duration::from_secs(86400);

// Check the literal authority too: URL parsers normalize integer/short IPv4 hosts.
pub fn local_endpoint(value: &str) -> Option<tauri::Url> {
    let rest = value.strip_prefix("http://127.0.0.1:")?;
    let (port, _) = rest.split_once('/')?;
    if port.is_empty()
        || !port.bytes().all(|c| c.is_ascii_digit())
        || port.parse::<u16>().ok()? == 0
    {
        return None;
    }
    let url = tauri::Url::parse(value).ok()?;
    (url.host_str() == Some("127.0.0.1")
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none())
    .then_some(url)
}

pub fn enabled(exe_dir: Option<&Path>, pubkey: &str, trial: bool) -> bool {
    !trial && !pubkey.trim().is_empty() && exe_dir.is_some_and(|p| p.join("app/cbs").is_dir())
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    current_version: String,
    available_version: Option<String>,
    notes: String,
    checking: bool,
    phase: String,
    downloaded: u64,
    total: Option<u64>,
    error: Option<String>,
}
struct Inner {
    status: Status,
    available: Option<Update>,
    last_check: Option<Instant>,
}
pub struct Updates {
    pub enabled: bool,
    inner: Mutex<Inner>,
    installing: AtomicBool,
}
pub fn installing(app: &tauri::AppHandle) -> bool {
    app.state::<Updates>().installing.load(Ordering::SeqCst)
}
impl Updates {
    pub fn new(enabled: bool, version: String) -> Self {
        Self {
            enabled,
            inner: Mutex::new(Inner {
                status: Status {
                    current_version: version,
                    available_version: None,
                    notes: String::new(),
                    checking: false,
                    phase: "idle".into(),
                    downloaded: 0,
                    total: None,
                    error: None,
                },
                available: None,
                last_check: None,
            }),
            installing: AtomicBool::new(false),
        }
    }
}
fn publish(app: &tauri::AppHandle) {
    let updates = app.state::<Updates>();
    let status = updates.inner.lock().unwrap().status.clone();
    let _ = app.emit_to("main", "resident-update", status);
}

// Called only after ui-ready. The monotonic timestamp deduplicates navigation and
// repeated readiness calls, and the native worker keeps checking when hidden.
pub fn start(app: &tauri::AppHandle) {
    let updates = app.state::<Updates>();
    if !updates.enabled {
        return;
    }
    {
        let mut inner = updates.inner.lock().unwrap();
        if inner.last_check.is_some() {
            return;
        }
        inner.last_check = Some(Instant::now());
    }
    let app = app.clone();
    std::thread::spawn(move || loop {
        tauri::async_runtime::block_on(check(&app));
        std::thread::sleep(DAY);
        if app.state::<Arc<Resident>>().stopping.load(Ordering::SeqCst) {
            break;
        }
    });
}
async fn check(app: &tauri::AppHandle) {
    let updates = app.state::<Updates>();
    if updates.installing.load(Ordering::SeqCst) {
        return;
    }
    updates.inner.lock().unwrap().status.checking = true;
    publish(app);
    let cleanup_app = app.clone();
    let result = match app
        .updater_builder()
        .timeout(Duration::from_secs(30))
        .on_before_exit(move || {
            let resident = cleanup_app.state::<Arc<Resident>>();
            resident.stopping.store(true, Ordering::SeqCst);
            // supervise owns the backend/job and performs the same drain as Quit.
            while !resident.done.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(50));
            }
            super::corner::save_position(&cleanup_app);
            use tauri_plugin_window_state::AppHandleExt;
            let _ = cleanup_app.save_window_state(
                tauri_plugin_window_state::StateFlags::SIZE
                    | tauri_plugin_window_state::StateFlags::POSITION,
            );
            cleanup_app.cleanup_before_exit();
        })
        .build()
    {
        Ok(updater) => updater.check().await,
        Err(error) => Err(error),
    };
    {
        let mut inner = updates.inner.lock().unwrap();
        inner.status.checking = false;
        if let Ok(Some(mut update)) = result {
            // The Python runtime and dependencies make the installer much larger
            // than its manifest; do not apply the short check timeout to it.
            update.timeout = Some(Duration::from_secs(60 * 60));
            inner.status.available_version = Some(update.version.clone());
            inner.status.notes = update.body.clone().unwrap_or_default();
            inner.available = Some(update);
        }
    }
    publish(app);
}

#[tauri::command]
pub async fn resident_update_status(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
    updates: tauri::State<'_, Updates>,
) -> Result<Status, String> {
    authorized(&window, &state)?;
    Ok(updates.inner.lock().unwrap().status.clone())
}

// Production and the offline ordering test share this boundary. The download
// result already includes signature verification; no shutdown on its error path.
fn install_verified<T, E>(
    download: Result<T, E>,
    install: impl FnOnce(T) -> Result<(), E>,
) -> Result<(), E> {
    install(download?)
}

#[tauri::command]
pub async fn resident_update_install(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
    updates: tauri::State<'_, Updates>,
) -> Result<(), String> {
    authorized(&window, &state)?;
    if !updates.enabled || state.stopping.load(Ordering::SeqCst) {
        return Err("更新は現在利用できません。".into());
    }
    if updates.installing.swap(true, Ordering::SeqCst) {
        return Err("更新は実行中です。".into());
    }
    let update = updates.inner.lock().unwrap().available.clone();
    let Some(update) = update else {
        updates.installing.store(false, Ordering::SeqCst);
        return Err("更新できる版が見つかっていません。".into());
    };
    {
        let mut inner = updates.inner.lock().unwrap();
        inner.status.phase = "downloading".into();
        inner.status.error = None;
        inner.status.downloaded = 0;
    }
    let app = window.app_handle().clone();
    publish(&app);
    let downloaded = update
        .download(
            |chunk, total| {
                let mut inner = updates.inner.lock().unwrap();
                inner.status.downloaded += chunk as u64;
                inner.status.total = total;
                drop(inner);
                publish(&app);
            },
            || {},
        )
        .await
        .map_err(|error| {
            // The plugin's error carries no secrets; keep it in the shell log for diagnosis.
            super::log(&state, &format!("update-download-failed: {error}"));
            "ダウンロードまたは署名の確認に失敗しました。もう一度お試しください。".to_owned()
        });
    let result = install_verified(downloaded, |bytes| {
        updates.inner.lock().unwrap().status.phase = "installing".into();
        publish(&app);
        // install re-verifies, extracts, then invokes the shutdown hook before
        // launching NSIS /P /R. The plugin exits only after a successful launch.
        update
            .install(bytes)
            .map_err(|error| {
                super::log(&state, &format!("update-install-failed: {error}"));
                "更新を開始できませんでした。Awalia を起動し直してください。".to_owned()
            })
    });
    if let Err(error) = &result {
        let mut inner = updates.inner.lock().unwrap();
        inner.status.phase = "failed".into();
        inner.status.error = Some(error.clone());
    }
    updates.installing.store(false, Ordering::SeqCst);
    publish(&app);
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_literal_loopback_http_with_port_and_path() {
        assert!(local_endpoint("http://127.0.0.1:8123/latest.json").is_some());
        for value in [
            "https://127.0.0.1:8123/a",
            "http://localhost:8123/a",
            "http://127.1:8123/a",
            "http://2130706433:8123/a",
            "http://127.0.0.1/a",
            "http://127.0.0.1:0/a",
            "http://127.0.0.1:65536/a",
            "http://127.0.0.1:8123@evil/a",
            "http://127.0.0.1:8123/a#b",
            "http://127.0.0.1:8123",
        ] {
            assert!(local_endpoint(value).is_none(), "{value}");
        }
    }
    #[test]
    fn development_empty_key_and_trial_disable_updater() {
        let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../.tmp/r5-layout")
            .join(std::process::id().to_string());
        assert!(!enabled(Some(&root), "public-fixture", false));
        std::fs::create_dir_all(root.join("app/cbs")).unwrap();
        assert!(enabled(Some(&root), "public-fixture", false));
        assert!(!enabled(Some(&root), " \n", false));
        assert!(!enabled(Some(&root), "public-fixture", true));
        assert!(!enabled(None, "public-fixture", false));
        assert!(root.starts_with(
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.tmp/r5-layout")
        ));
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn download_and_signature_failure_never_stop_backend() {
        let events = std::cell::RefCell::new(vec![]);
        for failure in ["download", "signature"] {
            let result = install_verified(Err::<Vec<u8>, _>(failure), |_| {
                events.borrow_mut().push("shutdown");
                Ok(())
            });
            assert_eq!(result, Err(failure));
            assert!(events.borrow().is_empty());
        }
        events.borrow_mut().push("download+verify");
        install_verified(Ok::<_, &str>(vec![1]), |_| {
            events
                .borrow_mut()
                .extend(["shutdown", "installer", "exit"]);
            Ok(())
        })
        .unwrap();
        assert_eq!(
            *events.borrow(),
            ["download+verify", "shutdown", "installer", "exit"]
        );
    }
}
