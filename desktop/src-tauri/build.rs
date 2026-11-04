fn main() {
    // The UI is served from the local backend (a remote origin for Tauri), so the
    // app's own commands need generated permissions to be granted in the capability.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "resident_context",
            "resident_opening",
            "resident_take_talk",
            "resident_ptt_state",
            "resident_ptt_preferences",
            "resident_notify",
            "resident_ui_ready",
            "resident_update_status",
            "resident_update_install",
            "resident_restore_restart",
            "resident_pick_folder",
            "resident_preferences",
            "resident_sync",
            "resident_corner_ready",
            "resident_corner_open",
            "resident_corner_drag",
        ]),
    ))
    .expect("failed to run tauri-build");
}
