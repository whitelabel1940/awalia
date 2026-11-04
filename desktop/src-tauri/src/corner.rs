//! A tiny bundled page; no provider calls, tokens or conversation text on disk.
use crate::{authorized, log, show, toggle, Resident};
use serde::{Deserialize, Serialize};
use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager, PhysicalPosition, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

fn neutral_expression() -> String {
    "neutral".into()
}

const HIDE_AFTER: Duration = Duration::from_secs(12);

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields, default)]
pub struct Preferences {
    pub always_visible: bool,
    pub shortcut: Option<String>,
    pub face_size: u32,
    pub chime_enabled: bool,
    pub chime_volume: u8,
    pub idle_motion: Option<bool>,
}
impl Default for Preferences {
    fn default() -> Self {
        Self {
            idle_motion: None,
            face_size: 96,
            chime_enabled: true,
            chime_volume: 40,
            always_visible: false,
            shortcut: Some("Ctrl+Alt+A".into()),
        }
    }
}
fn patch_preferences(
    current: &Preferences,
    patch: serde_json::Value,
) -> Result<Preferences, String> {
    let error = || "常駐の設定を確認してください。".to_owned();
    let patch = patch.as_object().ok_or_else(error)?;
    let mut value = serde_json::to_value(current).map_err(|_| error())?;
    for (key, next) in patch {
        value
            .as_object_mut()
            .unwrap()
            .insert(key.clone(), next.clone());
    }
    serde_json::from_value(value).map_err(|_| error())
}
#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Position {
    monitor: Option<String>,
    x: f64,
    y: f64,
}
#[derive(Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
struct Saved {
    preferences: Preferences,
    position: Option<Position>,
}
#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Face {
    character_id: String,
    name: String,
    expression: String,
    revision: i64,
    quiet: bool,
    valid_for_ms: u64,
    #[serde(default = "neutral_expression")]
    fallback_expression: String,
    #[serde(default)]
    reply_remaining_ms: Option<u64>,
}
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Target {
    pub turn_id: i64,
    pub session_id: String,
    pub character_id: String,
}
struct Speech {
    allow_quiet: bool,
    outside_talk: bool,
    body: String,
    target: Target,
    until: Instant,
    chimed: bool,
}
struct Runtime {
    saved: Saved,
    registered: Option<Shortcut>,
    shortcut_error: Option<String>,
    face: Option<Face>,
    valid_until: Instant,
    reply_until: Option<Instant>,
    speech: Option<Speech>,
    ready: bool,
    rendered: String,
    region: Option<(bool, u64, u32)>,
    layout_size: u32,
    last_position: Option<PhysicalPosition<i32>>,
    moved_at: Option<Instant>,
}
impl Runtime {
    // Consume only after successful rendering/region/show, even if sound is disabled.
    fn take_chime(&mut self) -> Option<u8> {
        let speech = self.speech.as_mut()?;
        if speech.chimed {
            return None;
        }
        speech.chimed = true;
        let prefs = &self.saved.preferences;
        (prefs.chime_enabled && prefs.chime_volume > 0).then_some(prefs.chime_volume)
    }
    fn present(&mut self, now: Instant, foreground: bool, fullscreen: bool) -> bool {
        if self.reply_until.is_some_and(|until| until <= now) {
            if let Some(face) = self.face.as_mut() {
                face.expression = face.fallback_expression.clone();
            }
            self.reply_until = None;
        }
        if self
            .speech
            .as_ref()
            .is_some_and(|s| s.until <= now || (foreground && !s.outside_talk))
        {
            self.speech = None;
        }
        let suppressed = fullscreen
            || now >= self.valid_until
            || self
                .face
                .as_ref()
                .is_none_or(|f| f.quiet && !self.speech.as_ref().is_some_and(|s| s.allow_quiet));
        if suppressed {
            self.speech = None;
        }
        self.ready
            && !suppressed
            && (self.speech.is_some() || self.saved.preferences.always_visible)
    }
}
pub struct Corner {
    path: PathBuf,
    inner: Mutex<Runtime>,
    preferences_gate: Mutex<()>,
}
impl Corner {
    pub fn load(data: &std::path::Path) -> Self {
        let path = data.join("desktop-partner.json");
        let mut saved: Saved = std::fs::read(&path)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default();
        if validate_preferences(&saved.preferences).is_err() {
            saved.preferences.face_size = 96;
            saved.preferences.chime_volume = 40;
        }
        Self {
            path,
            preferences_gate: Mutex::new(()),
            inner: Mutex::new(Runtime {
                saved,
                registered: None,
                shortcut_error: None,
                face: None,
                valid_until: Instant::now(),
                reply_until: None,
                speech: None,
                ready: false,
                rendered: String::new(),
                region: None,
                layout_size: 0,
                last_position: None,
                moved_at: None,
            }),
        }
    }
    fn save(&self, saved: &Saved) -> Result<(), String> {
        let bytes =
            serde_json::to_vec_pretty(saved).map_err(|_| "常駐の設定を保存できませんでした。")?;
        let temp = self.path.with_extension("json.tmp");
        std::fs::write(&temp, bytes)
            .and_then(|_| std::fs::rename(&temp, &self.path))
            .map_err(|_| "常駐の設定を保存できませんでした。".into())
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PreferenceStatus {
    preferences: Preferences,
    shortcut_error: Option<String>,
}

// Async commands keep plugin registration (which dispatches to the UI thread) off it.
#[tauri::command]
pub async fn resident_preferences(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
    corner: tauri::State<'_, Corner>,
    preferences: Option<serde_json::Value>,
) -> Result<PreferenceStatus, String> {
    authorized(&window, &state)?;
    // Never hold the render mutex across a plugin call: the UI thread also uses it.
    let _gate = corner.preferences_gate.lock().unwrap();
    if let Some(patch) = preferences {
        let mut prefs = patch_preferences(&corner.inner.lock().unwrap().saved.preferences, patch)?;
        validate_preferences(&prefs)?;
        prefs.shortcut = prefs.shortcut.map(|s| s.trim().to_owned());
        let next = parse_shortcut(&prefs.shortcut)?;
        if next.is_some() && next == window.state::<crate::ptt::Ptt>().key() {
            return Err("押して話すキーと別のキーを選んでください。".into());
        }
        let (old, previous) = {
            let inner = corner.inner.lock().unwrap();
            (inner.registered, inner.saved.preferences.clone())
        };
        if next != old {
            if let Some(key) = next {
                register(window.app_handle(), key)?;
            }
            if let Some(key) = old {
                if let Err(error) = unregister(window.app_handle(), key) {
                    if let Some(key) = next {
                        let _ = unregister(window.app_handle(), key);
                    }
                    return Err(error);
                }
            }
        }
        let saved = {
            let mut inner = corner.inner.lock().unwrap();
            inner.saved.preferences = prefs;
            let result = corner.save(&inner.saved);
            if result.is_err() {
                inner.saved.preferences = previous;
            }
            result
        };
        if let Err(error) = saved {
            if next != old {
                if let Some(key) = next {
                    let _ = unregister(window.app_handle(), key);
                }
                if let Some(key) = old {
                    if let Err(reason) = register(window.app_handle(), key) {
                        let mut inner = corner.inner.lock().unwrap();
                        inner.registered = None;
                        inner.shortcut_error = Some(reason);
                    }
                }
            }
            return Err(error);
        }
        let mut inner = corner.inner.lock().unwrap();
        inner.registered = next;
        inner.shortcut_error = None;
    }
    let inner = corner.inner.lock().unwrap();
    Ok(PreferenceStatus {
        preferences: inner.saved.preferences.clone(),
        shortcut_error: inner.shortcut_error.clone(),
    })
}
fn validate_preferences(prefs: &Preferences) -> Result<(), String> {
    if ![64, 96, 128].contains(&prefs.face_size) || prefs.chime_volume > 100 {
        return Err("顔の大きさと音量を確認してください。".into());
    }
    Ok(())
}
fn dimensions(face_size: u32) -> (f64, f64) {
    (316.0 + face_size as f64, (face_size as f64 + 24.0).max(140.0))
}
fn fitted_dimensions(face_size: u32, dpi: f64, work_width: u32, work_height: u32) -> (u32, u32) {
    let (width, height) = dimensions(face_size);
    let fit = (work_width as f64 / (width * dpi))
        .min(work_height as f64 / (height * dpi))
        .min(1.0);
    (
        (width * dpi * fit).floor() as u32,
        (height * dpi * fit).floor() as u32,
    )
}
fn unregister(app: &tauri::AppHandle, key: Shortcut) -> Result<(), String> {
    app.global_shortcut()
        .unregister(key)
        .map_err(|_| "前のショートカットを解除できませんでした。".into())
}

fn parse_shortcut(value: &Option<String>) -> Result<Option<Shortcut>, String> {
    value
        .as_ref()
        .map(|s| {
            s.parse()
                .map_err(|_| "キーの指定を確認してください（例: Ctrl+Alt+A）。".into())
        })
        .transpose()
}
fn register(app: &tauri::AppHandle, shortcut: Shortcut) -> Result<(), String> {
    app.global_shortcut()
        .on_shortcut(shortcut, |app, _, event| {
            // Release only: holding a key must not toggle the window repeatedly.
            if event.state == ShortcutState::Released {
                toggle(app);
                if let Some(window) = app.get_webview_window("main") {
                    if window.is_visible().unwrap_or(false) {
                        let _ = window.emit("resident-focus-input", ());
                    }
                }
            }
        })
        .map_err(|_| "このキーは登録できません。他のアプリが使用中の可能性があります。".into())
}

#[tauri::command]
pub async fn resident_sync(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
    corner: tauri::State<'_, Corner>,
    face: Face,
) -> Result<(), String> {
    authorized(&window, &state)?;
    if !valid_id(&face.character_id)
        || ![
            "neutral",
            "smile",
            "shy",
            "surprised",
            "sad",
            "troubled",
            "thinking",
            "pout",
        ]
        .contains(&face.expression.as_str())
    {
        return Err("表情を確認できません。".into());
    }
    let mut inner = corner.inner.lock().unwrap();
    if inner
        .face
        .as_ref()
        .is_some_and(|old| old.character_id != face.character_id)
    {
        inner.speech = None;
    }
    if ![
        "neutral",
        "smile",
        "shy",
        "surprised",
        "sad",
        "troubled",
        "thinking",
        "pout",
    ]
    .contains(&face.fallback_expression.as_str())
    {
        return Err("表情を確認できません。".into());
    }
    inner.reply_until = face
        .reply_remaining_ms
        .map(|ms| Instant::now() + Duration::from_millis(ms.min(86400000)));
    inner.valid_until = Instant::now() + Duration::from_millis(face.valid_for_ms.min(15000));
    inner.face = Some(face);
    Ok(())
}
fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
}
pub fn notify(
    app: &tauri::AppHandle,
    body: String,
    target: Target,
    allow_quiet: bool,
    outside_talk: bool,
) {
    let corner = app.state::<Corner>();
    let mut inner = corner.inner.lock().unwrap();
    // Suppressed turns are dropped, never queued to surprise the user later.
    if inner.valid_until <= Instant::now()
        || inner
            .face
            .as_ref()
            .is_none_or(|f| (f.quiet && !allow_quiet) || f.character_id != target.character_id)
        || fullscreen()
    {
        return;
    }
    inner.speech = Some(Speech {
        allow_quiet,
        outside_talk,
        body: body.chars().take(300).collect(),
        target,
        until: Instant::now() + HIDE_AFTER,
        chimed: false,
    });
}
pub fn dismiss(app: &tauri::AppHandle) {
    app.state::<Corner>().inner.lock().unwrap().speech = None;
    if let Some(window) = app.get_webview_window("corner") {
        hide_corner(&window);
    }
}
fn hide_corner(window: &WebviewWindow) {
    let _ = window.hide();
    let _ = window.emit("resident-corner-visibility", false);
}
pub fn save_position(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("corner") {
        if let Some(position) = remember(&window) {
            let corner = app.state::<Corner>();
            let mut inner = corner.inner.lock().unwrap();
            inner.saved.position = Some(position);
            if corner.save(&inner.saved).is_err() {
                log(&app.state::<Arc<Resident>>(), "corner-position-save-failed");
            }
        }
    }
}
fn authorized_corner(window: &WebviewWindow) -> Result<(), String> {
    let url = window.url().map_err(|_| "画面を確認できません。")?;
    if window.label() == "corner"
        && (url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost"))
        && url.path() == "/corner.html"
    {
        Ok(())
    } else {
        Err("端の窓からだけ使えます。".into())
    }
}
#[tauri::command]
pub async fn resident_corner_ready(
    window: WebviewWindow,
    corner: tauri::State<'_, Corner>,
) -> Result<(), String> {
    authorized_corner(&window)?;
    let mut inner = corner.inner.lock().unwrap();
    inner.ready = true;
    inner.rendered.clear();
    log(&window.state::<Arc<Resident>>(), "corner-ready");
    Ok(())
}
#[tauri::command]
pub async fn resident_corner_open(
    window: WebviewWindow,
    corner: tauri::State<'_, Corner>,
) -> Result<(), String> {
    authorized_corner(&window)?;
    let target = corner
        .inner
        .lock()
        .unwrap()
        .speech
        .as_ref()
        .filter(|s| s.until > Instant::now())
        .map(|s| s.target.clone());
    if let Some(target) = &target {
        *window.state::<Arc<Resident>>().talk_target.lock().unwrap() = Some(target.clone());
    }
    show(window.app_handle());
    if let Some(target) = target {
        let _ = window
            .app_handle()
            .emit_to("main", "resident-open-talk", target);
    } else {
        let _ = window
            .app_handle()
            .emit_to("main", "resident-focus-input", ());
    }
    Ok(())
}
#[tauri::command]
pub async fn resident_corner_drag(window: WebviewWindow) -> Result<(), String> {
    authorized_corner(&window)?;
    window
        .start_dragging()
        .map_err(|_| "窓を動かせませんでした。".into())
}

fn ratio(value: f64) -> f64 {
    if value.is_finite() {
        value.clamp(0.0, 1.0)
    } else {
        1.0
    }
}
fn placed(origin: i32, extent: u32, size: u32, fraction: f64) -> i32 {
    origin.saturating_add((extent.saturating_sub(size) as f64 * ratio(fraction)).round() as i32)
}
fn restore(window: &WebviewWindow, saved: &Option<Position>, face_size: u32) {
    let monitors = window.available_monitors().unwrap_or_default();
    let monitor = saved
        .as_ref()
        .and_then(|p| monitors.iter().find(|m| m.name() == p.monitor.as_ref()))
        .cloned()
        .or_else(|| window.primary_monitor().ok().flatten());
    if let Some(m) = monitor {
        let area = m.work_area();
        // Apply the destination DPI directly; very small work areas fit the whole layout.
        let (w, h) = fitted_dimensions(
            face_size,
            m.scale_factor(),
            area.size.width,
            area.size.height,
        );
        let _ = window.set_size(tauri::PhysicalSize::new(w, h));
        let (x, y) = saved.as_ref().map(|p| (p.x, p.y)).unwrap_or((1.0, 1.0));
        let _ = window.set_position(PhysicalPosition::new(
            placed(area.position.x, area.size.width, w, x),
            placed(area.position.y, area.size.height, h, y),
        ));
    }
}
fn remember(window: &WebviewWindow) -> Option<Position> {
    let m = window.current_monitor().ok().flatten()?;
    let pos = window.outer_position().ok()?;
    let size = window.outer_size().ok()?;
    let area = m.work_area();
    Some(Position {
        monitor: m.name().cloned(),
        x: ratio(
            (pos.x as f64 - area.position.x as f64)
                / area.size.width.saturating_sub(size.width).max(1) as f64,
        ),
        y: ratio(
            (pos.y as f64 - area.position.y as f64)
                / area.size.height.saturating_sub(size.height).max(1) as f64,
        ),
    })
}
// Run UI work on its owning thread. The worker only schedules and sleeps.
fn tick(app: &tauri::AppHandle) {
    let Some(window) = app.get_webview_window("corner") else {
        return;
    };
    let state = app.state::<Arc<Resident>>();
    let corner = app.state::<Corner>();
    let mut inner = corner.inner.lock().unwrap();
    let now = Instant::now();
    let foreground = app
        .get_webview_window("main")
        .is_some_and(|w| crate::visibility(&w, &state).foreground);
    let visible = inner.present(now, foreground, fullscreen());
    let bubble = inner.speech.is_some();
    let face_size = inner.saved.preferences.face_size;
    // Resize on the UI thread and restore the saved monitor-relative anchor.
    if inner.layout_size != face_size {
        restore(&window, &inner.saved.position, face_size);
        inner.region = None;
        inner.layout_size = face_size;
    }
    if visible {
        let scale = window.scale_factor().unwrap_or(1.0);
        let layout_scale = window
            .inner_size()
            .map(|s| s.width as f64 / scale / dimensions(face_size).0)
            .unwrap_or(1.0);
        let f = inner.face.as_ref().unwrap();
        let payload = serde_json::json!({"name":f.name,"imageUrl":format!("http://127.0.0.1:{}/looks/{}/{}?v={}-{}", state.options.port, f.character_id, f.expression, f.revision, f.expression),"body":inner.speech.as_ref().map(|s| &s.body),"faceSize":face_size,"layoutScale":layout_scale,"idleMotion":inner.saved.preferences.idle_motion,"neutralImageUrl":format!("http://127.0.0.1:{}/looks/{}/neutral?v={}", state.options.port, f.character_id, f.revision)});
        let rendered = payload.to_string();
        if rendered != inner.rendered {
            if window.emit("resident-corner", payload).is_err() {
                return;
            }
            inner.rendered = rendered;
        }
        let scale = scale * layout_scale;
        if inner.region != Some((bubble, scale.to_bits(), face_size)) {
            if !set_region(&window, bubble, scale, face_size) {
                hide_corner(&window);
                return;
            }
            inner.region = Some((bubble, scale.to_bits(), face_size));
        }
        if !window.is_visible().unwrap_or(false) {
            if window.show().is_err() {
                return;
            }
            let _ = window.emit("resident-corner-visibility", true);
        }
        if let Some(volume) = inner.take_chime() {
            crate::chime::play(volume);
        }
    } else if window.is_visible().unwrap_or(false) {
        hide_corner(&window);
    }
    let pos = window.outer_position().ok();
    if pos != inner.last_position {
        inner.last_position = pos;
        inner.moved_at = Some(now);
    }
    // Debounce disk writes and clamp after drag/DPI/topology changes, never during a drag.
    if inner
        .moved_at
        .is_some_and(|t| now.duration_since(t) > Duration::from_millis(700))
        && !dragging()
    {
        if let Some(p) = remember(&window) {
            inner.saved.position = Some(p);
            restore(
                &window,
                &inner.saved.position,
                inner.saved.preferences.face_size,
            );
            if corner.save(&inner.saved).is_err() {
                let _ = app.emit_to(
                    "main",
                    "resident-warning",
                    "端の窓の位置を保存できませんでした。",
                );
            }
        }
        inner.moved_at = None;
    }
    // A removed display can leave a window outside every work area without a move event.
    if let (Some(pos), Ok(size), Ok(monitors)) =
        (pos, window.outer_size(), window.available_monitors())
    {
        let inside = monitors.iter().any(|m| {
            let a = m.work_area();
            pos.x >= a.position.x
                && pos.y >= a.position.y
                && pos.x as i64 + size.width as i64 <= a.position.x as i64 + a.size.width as i64
                && pos.y as i64 + size.height as i64 <= a.position.y as i64 + a.size.height as i64
        });
        if !inside && inner.moved_at.is_none() && !dragging() {
            restore(
                &window,
                &inner.saved.position,
                inner.saved.preferences.face_size,
            );
        }
    }
}
pub fn setup(app: &tauri::App) -> tauri::Result<()> {
    let state = app.state::<Arc<Resident>>();
    let trial = state.options.first_run.is_some();
    let mut builder =
        WebviewWindowBuilder::new(app, "corner", WebviewUrl::App("corner.html".into()));
    builder = builder.data_directory(state.options.data_dir.join("webview"));
    let window = builder
        .title(if trial {
            "Awalia お試し パートナー"
        } else {
            "Awalia パートナー"
        })
        .inner_size(380.0, 140.0)
        .visible(false)
        .transparent(true)
        .decorations(false)
        .shadow(false)
        .always_on_top(true)
        .resizable(false)
        .skip_taskbar(true)
        .focused(false)
        .focusable(false)
        .disable_drag_drop_handler()
        // WebView2 shares one environment per profile: the options must match the main window's.
        .additional_browser_args(crate::WEBVIEW_ARGS)
        .build()?;
    let corner = app.state::<Corner>();
    let mut inner = corner.inner.lock().unwrap();
    if trial && !corner.path.is_file() {
        // Do not compete with the real resident's default global shortcut.
        inner.saved.preferences.shortcut = None;
    }
    restore(
        &window,
        &inner.saved.position,
        inner.saved.preferences.face_size,
    );
    match parse_shortcut(&inner.saved.preferences.shortcut).and_then(|key| {
        if let Some(key) = key {
            register(app.handle(), key)?;
        }
        Ok(key)
    }) {
        Ok(key) => inner.registered = key,
        Err(error) => inner.shortcut_error = Some(error),
    }
    crate::ptt::setup(app);
    let handle = app.handle().clone();
    let pending = Arc::new(std::sync::atomic::AtomicBool::new(false));
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(200));
        if handle
            .state::<Arc<Resident>>()
            .stopping
            .load(std::sync::atomic::Ordering::SeqCst)
        {
            break;
        }
        // Native dragging can block the UI loop; keep at most one queued update.
        if pending.swap(true, std::sync::atomic::Ordering::SeqCst) {
            continue;
        }
        let app = handle.clone();
        let scheduled = pending.clone();
        if handle
            .run_on_main_thread(move || {
                tick(&app);
                scheduled.store(false, std::sync::atomic::Ordering::SeqCst);
            })
            .is_err()
        {
            break;
        }
    });
    Ok(())
}

#[cfg(windows)]
fn dragging() -> bool {
    unsafe { windows_sys::Win32::UI::Input::KeyboardAndMouse::GetAsyncKeyState(1) < 0 }
}
#[cfg(not(windows))]
fn dragging() -> bool {
    false
}
#[cfg(windows)]
fn fullscreen() -> bool {
    use windows_sys::Win32::{Foundation::RECT, Graphics::Gdi::*, UI::WindowsAndMessaging::*};
    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd.is_null()
            || hwnd == GetDesktopWindow()
            || hwnd == GetShellWindow()
            || IsIconic(hwnd) != 0
        {
            return false;
        }
        let mut rect: RECT = std::mem::zeroed();
        let mut monitor: MONITORINFO = std::mem::zeroed();
        monitor.cbSize = std::mem::size_of::<MONITORINFO>() as u32;
        let handle = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
        if GetWindowRect(hwnd, &mut rect) == 0 || GetMonitorInfoW(handle, &mut monitor) == 0 {
            return true;
        }
        // A normal maximized window retains its caption; borderless fullscreen does not.
        GetWindowLongW(hwnd, GWL_STYLE) as u32 & WS_CAPTION != WS_CAPTION
            && covers(rect, monitor.rcMonitor)
    }
}
#[cfg(not(windows))]
fn fullscreen() -> bool {
    false
}
#[cfg(windows)]
fn covers(
    a: windows_sys::Win32::Foundation::RECT,
    b: windows_sys::Win32::Foundation::RECT,
) -> bool {
    a.left <= b.left && a.top <= b.top && a.right >= b.right && a.bottom >= b.bottom
}
#[cfg(windows)]
fn set_region(window: &WebviewWindow, bubble: bool, scale: f64, face_size: u32) -> bool {
    use windows_sys::Win32::Graphics::Gdi::*;
    let Ok(hwnd) = window.hwnd() else {
        return false;
    };
    let px = |v: f64| (v * scale).round() as i32;
    unsafe {
        // Must match corner.html. Outside this union Windows passes hits to the app below.
        let size = face_size as f64;
        let bottom = dimensions(face_size).1 - 12.0;
        let region = CreateEllipticRgn(px(304.0), px(bottom - size), px(304.0 + size), px(bottom));
        if region.is_null() {
            return false;
        }
        if bubble {
            let speech =
                CreateRoundRectRgn(px(8.0), px(8.0), px(292.0), px(124.0), px(24.0), px(24.0));
            if speech.is_null() {
                DeleteObject(region);
                return false;
            }
            let combined = CombineRgn(region, region, speech, RGN_OR);
            DeleteObject(speech);
            if combined == ERROR {
                DeleteObject(region);
                return false;
            }
        }
        if SetWindowRgn(hwnd.0 as _, region, 1) == 0 {
            DeleteObject(region);
            return false;
        }
        // Windows now owns region.
        true
    }
}
#[cfg(not(windows))]
fn set_region(_: &WebviewWindow, _: bool, _: f64, _: u32) -> bool {
    true
}

#[cfg(test)]
mod tests {
    #[test]
    fn partial_preferences_preserve_other_values_and_validate_shape() {
        let current = super::Preferences {
            always_visible: true,
            face_size: 128,
            shortcut: Some("Ctrl+Alt+X".into()),
            idle_motion: Some(false),
            ..Default::default()
        };
        let next =
            super::patch_preferences(&current, serde_json::json!({"chimeEnabled": false})).unwrap();
        assert!(!next.chime_enabled);
        assert!(next.always_visible);
        assert_eq!(next.face_size, 128);
        assert_eq!(next.shortcut, current.shortcut);
        assert_eq!(next.idle_motion, Some(false));
        let next = super::patch_preferences(&next, serde_json::json!({"shortcut": null})).unwrap();
        assert_eq!(next.shortcut, None);
        assert!(super::patch_preferences(&current, serde_json::json!({"unknown": true})).is_err());
        assert!(
            super::patch_preferences(&current, serde_json::json!({"faceSize": "large"})).is_err()
        );
        assert!(super::patch_preferences(&current, serde_json::json!([])).is_err());
    }
    use super::*;
    #[test]
    fn old_preferences_keep_position_and_receive_m125_defaults() {
        let saved: Saved = serde_json::from_str(r#"{"preferences":{"alwaysVisible":true,"shortcut":null},"position":{"monitor":"second","x":0.75,"y":1.0}}"#).unwrap();
        assert!(saved.preferences.always_visible);
        assert_eq!(saved.preferences.shortcut, None);
        assert_eq!(saved.preferences.face_size, 96);
        assert!(saved.preferences.chime_enabled);
        assert_eq!(saved.preferences.chime_volume, 40);
        assert_eq!(saved.preferences.idle_motion, None);
        assert_eq!(saved.position.unwrap().x, 0.75);
        for size in [64, 96, 128] {
            let prefs = Preferences {
                face_size: size,
                ..Preferences::default()
            };
            assert!(validate_preferences(&prefs).is_ok());
            let (w, h) = dimensions(size);
            for dpi in [1.0, 1.5, 2.0] {
                let width = (w * dpi).round() as u32;
                let height = (h * dpi).round() as u32;
                assert!(placed(-1920, 1920, width, 1.0) as i64 + width as i64 <= 0);
                assert!(placed(0, 1040, height, 1.0) as i64 + height as i64 <= 1040);
            }
        }
        assert_eq!(dimensions(64), (380.0, 140.0));
        assert_eq!(dimensions(96), (412.0, 140.0));
        assert_eq!(dimensions(128), (444.0, 152.0));
        assert_eq!(fitted_dimensions(128, 2.0, 1280, 720), (888, 304));
        assert_eq!(fitted_dimensions(96, 1.5, 1920, 1040), (618, 210));
        assert_eq!(fitted_dimensions(128, 2.0, 444, 152), (444, 152));
        assert!(validate_preferences(&Preferences {
            face_size: 65,
            ..Preferences::default()
        })
        .is_err());
        assert!(validate_preferences(&Preferences {
            chime_volume: 101,
            ..Preferences::default()
        })
        .is_err());
    }
    #[test]
    fn placement_handles_negative_monitors_dpi_and_removed_space() {
        assert_eq!(placed(-2560, 2560, 570, 1.0), -570);
        assert_eq!(placed(-2560, 2560, 760, 0.5), -1660);
        assert_eq!(placed(0, 320, 380, 1.0), 0);
        assert_eq!(placed(0, 1920, 380, f64::NAN), 1540);
        assert_eq!(placed(0, 1920, 380, -4.0), 0);
    }
    #[test]
    fn default_and_disabled_shortcuts_and_invalid_format() {
        assert!(parse_shortcut(&Preferences::default().shortcut)
            .unwrap()
            .is_some());
        assert!(parse_shortcut(&None).unwrap().is_none());
        assert!(parse_shortcut(&Some("not+a+shortcut".into())).is_err());
    }
    #[test]
    fn speech_expires_but_optional_face_remains_and_main_focus_dismisses_speech() {
        let corner = Corner::load(std::path::Path::new(".tmp/m124/nonexistent"));
        let mut r = corner.inner.lock().unwrap();
        let now = Instant::now();
        r.ready = true;
        r.face = Some(Face {
            character_id: "haru".into(),
            name: "test".into(),
            expression: "smile".into(),
            revision: 1,
            quiet: false,
            valid_for_ms: 15000,
            fallback_expression: "neutral".into(),
            reply_remaining_ms: None,
        });
        r.valid_until = now + Duration::from_secs(15);
        r.speech = Some(Speech {
            allow_quiet: false,
            outside_talk: false,
            body: "test".into(),
            target: Target {
                turn_id: 1,
                session_id: "test".into(),
                character_id: "haru".into(),
            },
            until: now + HIDE_AFTER,
            chimed: false,
        });
        r.face.as_mut().unwrap().fallback_expression = "thinking".into();
        r.reply_until = Some(now + Duration::from_secs(2));
        assert!(r.present(now, false, false));
        assert_eq!(r.face.as_ref().unwrap().expression, "smile");
        assert!(r.present(now + Duration::from_secs(2), false, false));
        assert_eq!(r.face.as_ref().unwrap().expression, "thinking");
        assert!(r.reply_until.is_none());
        assert_eq!(r.take_chime(), Some(40));
        assert_eq!(
            r.take_chime(),
            None,
            "render/face/settings updates never replay a chime"
        );
        r.speech.as_mut().unwrap().chimed = false;
        r.saved.preferences.chime_enabled = false;
        assert_eq!(r.take_chime(), None);
        r.saved.preferences.chime_enabled = true;
        assert_eq!(
            r.take_chime(),
            None,
            "enabling sound does not replay an old bubble"
        );
        r.speech.as_mut().unwrap().chimed = false;
        r.saved.preferences.chime_volume = 0;
        assert_eq!(r.take_chime(), None);
        // A foreground wizard has no chat transcript: keep its bubble clickable.
        r.speech.as_mut().unwrap().outside_talk = true;
        assert!(r.present(now, true, false));
        assert!(r.speech.is_some());
        assert!(!r.present(now + HIDE_AFTER, false, false));
        r.saved.preferences.always_visible = true;
        assert!(r.present(now + HIDE_AFTER, true, false));
        assert!(!r.present(now + Duration::from_secs(15), false, false));
        // HB1: quiet permission is scoped to this speech, not the persistent face.
        r.saved.preferences.chime_volume = 40;
        r.face.as_mut().unwrap().quiet = true;
        r.speech = Some(Speech {
            allow_quiet: true,
            outside_talk: false,
            body: "habit".into(),
            target: Target {
                turn_id: 2,
                session_id: "test".into(),
                character_id: "haru".into(),
            },
            until: now + HIDE_AFTER,
            chimed: false,
        });
        assert!(r.present(now, false, false));
        assert_eq!(r.take_chime(), Some(40));
        assert!(!r.present(now, false, true));
        assert!(r.speech.is_none());
        assert!(!r.present(now, false, false));
    }
    #[test]
    fn suppressed_speech_is_dropped_and_never_replayed_after_fullscreen_or_quiet() {
        let corner = Corner::load(std::path::Path::new(".tmp/m124/nonexistent"));
        let mut r = corner.inner.lock().unwrap();
        let now = Instant::now();
        r.ready = true;
        r.valid_until = now + Duration::from_secs(15);
        r.face = Some(Face {
            character_id: "haru".into(),
            name: "test".into(),
            expression: "smile".into(),
            revision: 1,
            quiet: false,
            valid_for_ms: 15000,
            fallback_expression: "neutral".into(),
            reply_remaining_ms: None,
        });
        for (quiet, fullscreen, foreground) in [
            (false, true, false),
            (true, false, false),
            (false, false, true),
        ] {
            r.face.as_mut().unwrap().quiet = quiet;
            r.speech = Some(Speech {
                allow_quiet: false,
                outside_talk: false,
                body: "test".into(),
                target: Target {
                    turn_id: 1,
                    session_id: "test".into(),
                    character_id: "haru".into(),
                },
                until: now + HIDE_AFTER,
                chimed: false,
            });
            assert!(!r.present(now, foreground, fullscreen));
            assert!(r.speech.is_none());
            assert_eq!(r.take_chime(), None, "suppressed speech has no sound");
            r.face.as_mut().unwrap().quiet = false;
            assert!(!r.present(now, false, false));
        }
    }
    #[cfg(windows)]
    #[test]
    fn fullscreen_rect_requires_the_whole_monitor() {
        use windows_sys::Win32::Foundation::RECT;
        let monitor = RECT {
            left: -1920,
            top: 0,
            right: 0,
            bottom: 1080,
        };
        assert!(covers(monitor, monitor));
        assert!(!covers(
            RECT {
                bottom: 1040,
                ..monitor
            },
            monitor
        ));
    }
}
