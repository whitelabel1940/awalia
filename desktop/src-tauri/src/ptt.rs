//! Independent push-to-talk registration; no default key and no captured audio.
use crate::{authorized, show, Resident};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use tauri::{Emitter, Manager, WebviewWindow};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

pub struct Ptt {
    path: std::path::PathBuf,
    key: Mutex<Option<Shortcut>>,
    held: AtomicBool,
    error: Mutex<Option<String>>,
}
impl Ptt {
    pub fn load(data: &std::path::Path) -> Self {
        Self {
            path: data.join("desktop-ptt.json"),
            key: Mutex::new(None),
            held: AtomicBool::new(false),
            error: Mutex::new(None),
        }
    }
    pub fn key(&self) -> Option<Shortcut> {
        *self.key.lock().unwrap()
    }
}
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    shortcut: Option<String>,
    error: Option<String>,
}

fn register(app: &tauri::AppHandle, key: Shortcut) -> Result<(), String> {
    if app.global_shortcut().is_registered(key) {
        return Err("呼び出しのキーと別のキーを選んでください。".into());
    }
    app.global_shortcut()
        .on_shortcut(key, |app, _, event| {
            let state = app.state::<Ptt>();
            let down = event.state == ShortcutState::Pressed;
            if state.held.swap(down, Ordering::SeqCst) == down {
                return;
            }
            if down {
                show(app);
            }
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.emit("resident-ptt", down);
            }
        })
        .map_err(|_| {
            "押して話すキーを登録できません。他のアプリで使用中か確認してください。".into()
        })
}

pub fn setup(app: &tauri::App) {
    let ptt = app.state::<Ptt>();
    let saved = std::fs::read(&ptt.path)
        .ok()
        .and_then(|data| serde_json::from_slice::<Option<String>>(&data).ok())
        .flatten();
    if let Some(text) = saved {
        match text
            .parse::<Shortcut>()
            .map_err(|_| "押して話すキーの形式を確認してください。".into())
            .and_then(|key| {
                register(app.handle(), key)?;
                Ok(key)
            }) {
            Ok(key) => *ptt.key.lock().unwrap() = Some(key),
            Err(error) => *ptt.error.lock().unwrap() = Some(error),
        }
    }
}

#[tauri::command]
pub async fn resident_ptt_state(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
    ptt: tauri::State<'_, Ptt>,
) -> Result<bool, String> {
    authorized(&window, &state)?;
    Ok(ptt.held.load(Ordering::SeqCst))
}

#[tauri::command]
pub async fn resident_ptt_preferences(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Resident>>,
    ptt: tauri::State<'_, Ptt>,
    shortcut: Option<String>,
    save: Option<bool>,
) -> Result<Status, String> {
    authorized(&window, &state)?;
    let mut current = ptt.key.lock().unwrap();
    if save.unwrap_or(false) {
        let text = shortcut
            .map(|s| s.trim().to_owned())
            .filter(|s| !s.is_empty());
        if text.as_ref().is_some_and(|s| s.len() > 100) {
            return Err("押して話すキーの指定を確認してください。".into());
        }
        let next = text
            .as_ref()
            .map(|s| {
                s.parse::<Shortcut>()
                    .map_err(|_| "押して話すキーの形式を確認してください。".to_owned())
            })
            .transpose()?;
        if *current != next {
            if let Some(key) = next {
                register(window.app_handle(), key)?;
            }
            if let Some(key) = *current {
                if window
                    .app_handle()
                    .global_shortcut()
                    .unregister(key)
                    .is_err()
                {
                    if let Some(key) = next {
                        let _ = window.app_handle().global_shortcut().unregister(key);
                    }
                    return Err("前の押して話すキーを解除できませんでした。".into());
                }
            }
            let temp = ptt.path.with_extension("json.tmp");
            let saved = std::fs::write(&temp, serde_json::to_vec(&text).unwrap())
                .and_then(|_| std::fs::rename(&temp, &ptt.path));
            if saved.is_err() {
                if let Some(key) = next {
                    let _ = window.app_handle().global_shortcut().unregister(key);
                }
                if let Some(key) = *current {
                    if let Err(error) = register(window.app_handle(), key) {
                        *current = None;
                        *ptt.error.lock().unwrap() = Some(error);
                    }
                }
                return Err("押して話すキーを保存できませんでした。".into());
            }
            *current = next;
            ptt.held.store(false, Ordering::SeqCst);
            let _ = window.emit("resident-ptt", false);
        }
        *ptt.error.lock().unwrap() = None;
    }
    Ok(Status {
        shortcut: current.map(|k| k.to_string()),
        error: ptt.error.lock().unwrap().clone(),
    })
}
