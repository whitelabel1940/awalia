#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod chime;
mod corner;
mod options;
#[cfg(windows)]
mod process;
mod ptt;
mod update;

use options::{restart_delay, Options, SHUTDOWN_TIMEOUT, STARTUP_TIMEOUT};
const WEBVIEW_ARGS: &str = "--disable-background-timer-throttling --disable-renderer-backgrounding";
use serde::Serialize;
use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::{
    Emitter, Manager, RunEvent, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_window_state::{AppHandleExt, StateFlags};

#[tauri::command]
async fn resident_pick_folder(window: WebviewWindow) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    if window.label() != "main" {
        return Err("メイン画面から選んでください。".into());
    }
    let folder = window
        .dialog()
        .file()
        .set_parent(&window)
        .blocking_pick_folder();
    folder
        .map(|path| {
            path.into_path()
                .map(|p| p.to_string_lossy().into_owned())
                .map_err(|_| "フォルダのパスを取得できませんでした。".into())
        })
        .transpose()
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct Visibility {
    visible: bool,
    minimized: bool,
    focused: bool,
    foreground: bool,
    ever_shown: bool,
}
struct Resident {
    options: Options,
    token: String,
    stopping: AtomicBool,
    restore_restart: AtomicBool,
    done: AtomicBool,
    ever_shown: AtomicBool,
    opening_claimed: AtomicBool,
    talk_target: Mutex<Option<corner::Target>>,
    error: Mutex<Option<String>>,
    started: Instant,
}
fn visibility(window: &WebviewWindow, state: &Resident) -> Visibility {
    let visible = window.is_visible().unwrap_or(false);
    let minimized = window.is_minimized().unwrap_or(true);
    let focused = window.is_focused().unwrap_or(false);
    Visibility {
        visible,
        minimized,
        focused,
        foreground: visible && !minimized && focused,
        ever_shown: state.ever_shown.load(Ordering::SeqCst),
    }
}
fn publish_visibility(window: &WebviewWindow) {
    let state = window.state::<Arc<Resident>>();
    let _ = window.emit("resident-visibility", visibility(window, &state));
}
fn show(app: &tauri::AppHandle) {
    corner::dismiss(app);
    if let Some(window) = app.get_webview_window("main") {
        app.state::<Arc<Resident>>()
            .ever_shown
            .store(true, Ordering::SeqCst);
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
        publish_visibility(&window);
    }
}
fn hide(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
        publish_visibility(&window);
    }
}
fn toggle(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        if window.is_visible().unwrap_or(false) && !window.is_minimized().unwrap_or(false) {
            hide(app);
        } else {
            show(app);
        }
    }
}
fn log(state: &Resident, event: &str) {
    // Only fixed event names and elapsed times; no args, provider errors or tokens.
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(state.options.data_dir.join("desktop-resident.log"))
    {
        let _ = writeln!(file, "{}ms {event}", state.started.elapsed().as_millis());
    }
}
fn fail(app: &tauri::AppHandle, message: &str) {
    let state = app.state::<Arc<Resident>>();
    *state.error.lock().unwrap() = Some(message.to_owned());
    log(&state, message);
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.navigate(
            "http://tauri.localhost/index.html"
                .parse()
                .expect("splash URL"),
        );
        let _ = window.emit("resident-error", message);
    }
}
fn authorized(window: &WebviewWindow, state: &Resident) -> Result<(), String> {
    let url = window.url().map_err(|_| "画面を確認できません。")?;
    let local = url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port_or_known_default() == Some(state.options.port);
    let splash = url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost");
    if window.label() == "main" && (local || splash) {
        Ok(())
    } else {
        Err("常駐の画面からだけ使えます。".into())
    }
}
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Context {
    token: String,
    visible: bool,
    minimized: bool,
    focused: bool,
    foreground: bool,
    ever_shown: bool,
    opening_claimed: bool,
    elapsed_ms: u64,
    error: Option<String>,
}
#[tauri::command]
async fn resident_context(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
) -> Result<Context, String> {
    authorized(&window, &state)?;
    let visible = visibility(&window, &state);
    Ok(Context {
        token: state.token.clone(),
        visible: visible.visible,
        minimized: visible.minimized,
        focused: visible.focused,
        foreground: visible.foreground,
        ever_shown: visible.ever_shown,
        opening_claimed: state.opening_claimed.load(Ordering::SeqCst),
        elapsed_ms: state.started.elapsed().as_millis().min(u64::MAX as u128) as u64,
        error: state.error.lock().unwrap().clone(),
    })
}
#[tauri::command]
async fn resident_opening(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
    deferred: bool,
) -> Result<bool, String> {
    authorized(&window, &state)?;
    if deferred {
        // Only a quiet-hours deferral returns the launch's greeting opportunity.
        state.opening_claimed.store(false, Ordering::SeqCst);
        Ok(false)
    } else {
        Ok(state
            .opening_claimed
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok())
    }
}
#[tauri::command]
async fn resident_take_talk(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
) -> Result<Option<corner::Target>, String> {
    authorized(&window, &state)?;
    let target = state.talk_target.lock().unwrap().take();
    Ok(target)
}
#[tauri::command]
async fn resident_notify(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
    title: String,
    body: String,
    turn_id: i64,
    session_id: String,
    character_id: String,
    allow_quiet: Option<bool>,
    outside_talk: Option<bool>,
) -> Result<(), String> {
    authorized(&window, &state)?;
    let outside_talk =
        outside_talk.unwrap_or(false) && window.url().is_ok_and(|url| url.path() == "/wizard");
    if visibility(&window, &state).foreground && !outside_talk {
        return Ok(());
    }
    let _ = title;
    corner::notify(
        window.app_handle(),
        body,
        corner::Target {
            turn_id,
            session_id,
            character_id,
        },
        allow_quiet.unwrap_or(false),
        outside_talk,
    );
    Ok(())
}

#[tauri::command]
async fn resident_ui_ready(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
) -> Result<(), String> {
    authorized(&window, &state)?;
    log(&state, "ui-ready");
    update::start(window.app_handle());
    Ok(())
}

#[tauri::command]
async fn resident_restore_restart(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
) -> Result<(), String> {
    authorized(&window, &state)?;
    if state.stopping.load(Ordering::SeqCst) || !http(&state, "GET", "/api/resident/restore-ready")
    {
        return Err("戻す準備を確認できません。もう一度お試しください。".into());
    }
    state.restore_restart.store(true, Ordering::SeqCst);
    Ok(())
}

fn http(state: &Resident, method: &str, path: &str) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], state.options.port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(300)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(600)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(600)));
    if write!(stream, "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nX-Awalia-Desktop: {}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n", state.options.port, state.token).is_err() { return false; }
    let mut response = String::new();
    if stream.take(8192).read_to_string(&mut response).is_err() {
        return false;
    }
    if method == "POST" {
        response.starts_with("HTTP/1.1 202")
    } else {
        response.starts_with("HTTP/1.1 200") && response.contains("\"ready\":true")
    }
}
fn pause(state: &Resident, duration: Duration) {
    let until = Instant::now() + duration;
    while Instant::now() < until && !state.stopping.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(windows)]
fn supervise(app: tauri::AppHandle, state: Arc<Resident>) {
    let mut restarts = 0;
    let mut restoring = false;
    loop {
        if state.stopping.load(Ordering::SeqCst) {
            break;
        }
        if TcpListener::bind(("127.0.0.1", state.options.port)).is_err() {
            fail(&app, "port-in-use");
            break;
        }
        let backend = match process::Backend::spawn(&state.options, &state.token) {
            Ok(backend) => backend,
            Err(_) => {
                fail(&app, "python-start-failed");
                break;
            }
        };
        log(&state, "backend-started");
        // A multi-GB pre-restore snapshot and verification can exceed normal boot.
        let deadline = Instant::now()
            + if restoring {
                Duration::from_secs(30 * 60)
            } else {
                STARTUP_TIMEOUT
            };
        restoring = false;
        let mut ready = false;
        while Instant::now() < deadline
            && !state.stopping.load(Ordering::SeqCst)
            && !backend.exited()
        {
            if http(&state, "GET", "/api/resident/health") {
                ready = true;
                break;
            }
            pause(&state, Duration::from_millis(200));
        }
        if ready {
            corner::dismiss(&app);
            log(&state, "backend-ready");
            if let Some(window) = app.get_webview_window("main") {
                if window
                    .navigate(
                        format!("http://127.0.0.1:{}/", state.options.port)
                            .parse()
                            .unwrap(),
                    )
                    .is_err()
                {
                    fail(&app, "navigation-failed");
                }
            }
            let mut missed = 0;
            while !state.stopping.load(Ordering::SeqCst)
                && !state.restore_restart.load(Ordering::SeqCst)
                && !backend.exited()
            {
                pause(&state, Duration::from_secs(2));
                if state.stopping.load(Ordering::SeqCst) {
                    break;
                }
                if http(&state, "GET", "/api/resident/health") {
                    missed = 0;
                } else {
                    missed += 1;
                }
                if missed >= 5 {
                    log(&state, "health-timeout");
                    break;
                }
            }
        }
        // Even a health timeout requests a normal drain before closing the job.
        if !backend.exited() {
            let accepted = http(&state, "POST", "/api/resident/shutdown");
            log(
                &state,
                if accepted {
                    "shutdown-requested"
                } else {
                    "shutdown-unreachable"
                },
            );
            if !backend.wait(SHUTDOWN_TIMEOUT) {
                log(&state, "shutdown-deadline-job-cleanup");
            }
        }
        corner::dismiss(&app);
        drop(backend);
        if state.stopping.load(Ordering::SeqCst) {
            break;
        }
        if state.restore_restart.swap(false, Ordering::SeqCst) {
            log(&state, "backend-restore-restarting");
            restoring = true;
            continue;
        }
        if let Some(delay) = restart_delay(restarts) {
            restarts += 1;
            log(&state, "backend-restarting");
            pause(&state, delay);
        } else {
            fail(&app, "restart-limit");
            break;
        }
    }
    state.done.store(true, Ordering::SeqCst);
    if state.stopping.load(Ordering::SeqCst) && !update::installing(&app) {
        app.exit(0);
    }
}

fn main() {
    let mut options = match Options::load() {
        Ok(value) => value,
        Err(_) => return,
    };
    if options.prepare_first_run().is_err() {
        return;
    }
    let trial = options.first_run.is_some();
    let _trial_guard = if trial {
        let mut guard = std::fs::OpenOptions::new();
        guard.create(true).write(true);
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            guard.share_mode(0);
        }
        match guard.open(options.data_dir.join(".shell-active")) {
            Ok(file) => Some(file),
            Err(_) => return,
        }
    } else {
        None
    };
    let _ = std::fs::create_dir_all(&options.data_dir);
    let _ = std::fs::create_dir_all(&options.characters);
    // Release builds have no console: keep the panic text where it can be found.
    let panic_log = options.data_dir.join("awalia-panic.log");
    std::panic::set_hook(Box::new(move |info| {
        use std::io::Write;
        if let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&panic_log)
        {
            let _ = writeln!(file, "{info}");
        }
    }));
    let mut random = [0u8; 32];
    if getrandom::fill(&mut random).is_err() {
        return;
    }
    let token = random.iter().map(|b| format!("{b:02x}")).collect();
    // Restoring MAXIMIZED can show a hidden native window on Windows. Only the
    // requested size/position are restored; visibility belongs to this shell.
    let flags = StateFlags::SIZE | StateFlags::POSITION;
    let args = options.autostart_args();
    let state = Arc::new(Resident {
        ever_shown: AtomicBool::new(!options.background),
        opening_claimed: AtomicBool::new(false),
        talk_target: Mutex::new(None),
        options,
        token,
        stopping: AtomicBool::new(false),
        restore_restart: AtomicBool::new(false),
        done: AtomicBool::new(false),
        error: Mutex::new(None),
        started: Instant::now(),
    });
    let corner_state = corner::Corner::load(&state.options.data_dir);
    let ptt_state = ptt::Ptt::load(&state.options.data_dir);
    let managed = state.clone();
    let mut context = tauri::generate_context!();
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(std::path::Path::to_path_buf));
    let pubkey = context
        .config()
        .plugins
        .0
        .get("updater")
        .and_then(|c| c.get("pubkey"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let update_enabled = update::enabled(exe_dir.as_deref(), pubkey, trial);
    if update_enabled {
        if let Some(endpoint) = std::env::var("AWALIA_UPDATE_ENDPOINT")
            .ok()
            .and_then(|v| update::local_endpoint(&v))
        {
            let config = context.config_mut().plugins.0.get_mut("updater").unwrap();
            config["endpoints"] = serde_json::json!([endpoint.as_str()]);
            config["dangerousInsecureTransportProtocol"] = serde_json::json!(true);
        }
    }
    let mut builder = tauri::Builder::default();
    if update_enabled {
        builder = builder.plugin(tauri_plugin_updater::Builder::new().build());
    }
    if !trial {
        builder = builder
            .plugin(tauri_plugin_single_instance::init(|app, _, _| show(app)))
            .plugin(tauri_plugin_autostart::Builder::new().args(args).build())
            .plugin(
                tauri_plugin_window_state::Builder::default()
                    .with_filename(
                        state
                            .options
                            .data_dir
                            .join("window-state.json")
                            .to_string_lossy(),
                    )
                    .with_state_flags(flags)
                    .with_denylist(&["corner"])
                    .build(),
            );
    }
    let app = builder
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(corner_state)
        .manage(ptt_state)
        .manage(managed)
        .manage(update::Updates::new(
            update_enabled,
            context.package_info().version.to_string(),
        ))
        .invoke_handler(tauri::generate_handler![
            resident_context,
            resident_opening,
            resident_take_talk,
            resident_notify,
            resident_ui_ready,
            update::resident_update_status,
            update::resident_update_install,
            resident_restore_restart,
            resident_pick_folder,
            ptt::resident_ptt_state,
            ptt::resident_ptt_preferences,
            corner::resident_preferences,
            corner::resident_sync,
            corner::resident_corner_ready,
            corner::resident_corner_open,
            corner::resident_corner_drag
        ])
        .setup(move |app| {
            use tauri::{
                menu::{CheckMenuItem, Menu, MenuItem},
                tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
            };
            let mut window_builder =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()));
            window_builder = window_builder.data_directory(state.options.data_dir.join("webview"));
            let window = window_builder
                .title(if trial { "Awalia お試し" } else { "Awalia" })
                .inner_size(1100.0, 780.0)
                .min_inner_size(360.0, 480.0)
                .visible(!state.options.background)
                // Let the page's own HTML drop zones (look images, voice WAVs) receive files.
                .disable_drag_drop_handler()
                .additional_browser_args(WEBVIEW_ARGS)
                .on_permission_request(|webview, kind| {
                    use tauri::webview::{PermissionKind, PermissionResponse};
                    if kind == PermissionKind::Microphone {
                        let state = webview.state::<Arc<Resident>>();
                        let local = webview.url().ok().is_some_and(|url| {
                            url.scheme() == "http"
                                && url.host_str() == Some("127.0.0.1")
                                && url.port_or_known_default() == Some(state.options.port)
                        });
                        if !local {
                            return PermissionResponse::Deny;
                        }
                    }
                    // WebView2 owns the user's allow/deny prompt; never auto-grant capture.
                    PermissionResponse::Default
                })
                .background_throttling(tauri::utils::config::BackgroundThrottlingPolicy::Disabled)
                .on_page_load(|window, payload| {
                    if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished)
                        && payload.url().host_str() == Some("127.0.0.1")
                    {
                        log(&window.state::<Arc<Resident>>(), "web-page-loaded");
                    }
                })
                .build()?;
            let open = MenuItem::with_id(app, "open", "開く", true, None::<&str>)?;
            let hide_item = MenuItem::with_id(app, "hide", "隠す", true, None::<&str>)?;
            let auto = CheckMenuItem::with_id(
                app,
                "autostart",
                "ログイン時に起動",
                !trial,
                !trial && app.autolaunch().is_enabled().unwrap_or(false),
                None::<&str>,
            )?;
            let quit = MenuItem::with_id(app, "quit", "終了", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &hide_item, &auto, &quit])?;
            TrayIconBuilder::with_id("resident")
                .tooltip(if trial { "Awalia お試し" } else { "Awalia" })
                .icon(tauri::image::Image::from_bytes(include_bytes!(
                    "../icons/app-light.png"
                ))?)
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "open" => show(app),
                    "hide" => hide(app),
                    "quit" => app.exit(0),
                    "autostart" => {
                        if trial {
                            return;
                        }
                        let enabled = app.autolaunch().is_enabled().unwrap_or(false);
                        let result = if enabled {
                            app.autolaunch().disable()
                        } else {
                            app.autolaunch().enable()
                        };
                        let _ = auto.set_checked(app.autolaunch().is_enabled().unwrap_or(false));
                        if result.is_err() {
                            let _ = app.get_webview_window("main").map(|w| {
                                w.emit(
                                    "resident-warning",
                                    "ログイン時の起動を変更できませんでした。",
                                )
                            });
                        }
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if matches!(
                        event,
                        TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        }
                    ) {
                        toggle(tray.app_handle());
                    }
                })
                .build(app)?;
            corner::setup(app)?;
            let event_window = window.clone();
            window.on_window_event(move |event| match event {
                WindowEvent::CloseRequested { api, .. } => {
                    api.prevent_close();
                    let _ = event_window.hide();
                    publish_visibility(&event_window);
                }
                WindowEvent::Focused(_) | WindowEvent::Resized(_) => {
                    publish_visibility(&event_window)
                }
                _ => {}
            });
            log(&state, "shell-ready");
            if !state.options.background {
                let _ = window.set_focus();
            }
            #[cfg(windows)]
            {
                let handle = app.handle().clone();
                let worker_state = state.clone();
                thread::spawn(move || supervise(handle, worker_state));
            }
            Ok(())
        })
        .build(context)
        .expect("Awalia shell initialization failed");
    app.run(move |app, event| {
        if let RunEvent::ExitRequested { api, .. } = event {
            let state = app.state::<Arc<Resident>>();
            corner::save_position(app);
            if state.options.first_run.is_none() {
                let _ = app.save_window_state(flags);
            }
            if !state.done.load(Ordering::SeqCst) {
                api.prevent_exit();
                state.stopping.store(true, Ordering::SeqCst);
            }
        }
    });
}
