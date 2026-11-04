use std::{
    env,
    ffi::OsString,
    path::{Path, PathBuf},
    time::Duration,
};

pub const MAX_RESTARTS: usize = 3;
pub const STARTUP_TIMEOUT: Duration = Duration::from_secs(45);
pub const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(25);

#[derive(Clone)]
pub struct Options {
    pub repo: PathBuf,
    pub python: PathBuf,
    pub port: u16,
    pub db: Option<PathBuf>,
    pub data_dir: PathBuf,
    pub characters: PathBuf,
    pub background: bool,
    pub first_run: Option<String>,
    pub first_run_source: Option<PathBuf>,
}

fn absolute(root: &Path, value: PathBuf) -> PathBuf {
    if value.is_absolute() {
        value
    } else {
        root.join(value)
    }
}

impl Options {
    pub fn load() -> Result<Self, String> {
        let default_repo = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let exe = env::current_exe().map_err(|_| "実行ファイルの場所を確認できません。")?;
        Self::parse_at(
            env::args_os().skip(1).collect(),
            |key| env::var_os(key),
            default_repo,
            exe.parent(),
        )
    }

    #[cfg(test)]
    fn parse(
        args: Vec<OsString>,
        get: impl Fn(&str) -> Option<OsString>,
        default_repo: PathBuf,
    ) -> Result<Self, String> {
        Self::parse_at(args, get, default_repo, None)
    }

    fn parse_at(
        args: Vec<OsString>,
        get: impl Fn(&str) -> Option<OsString>,
        default_repo: PathBuf,
        exe_dir: Option<&Path>,
    ) -> Result<Self, String> {
        let installed = exe_dir.filter(|dir| dir.join("app/cbs").is_dir());
        let default_repo = installed.map(|dir| dir.join("app")).unwrap_or(default_repo);
        let mut values = std::collections::HashMap::new();
        let mut background = false;
        let mut first_run = None;
        let mut iter = args.into_iter();
        while let Some(arg) = iter.next() {
            if arg == "--first-run" {
                first_run = Some("new".to_owned());
                continue;
            }
            if let Some(mode) = arg.to_str().and_then(|s| s.strip_prefix("--first-run=")) {
                if !["new", "keep", "bare"].contains(&mode) {
                    return Err("お試しの起動方法を確認してください。".into());
                }
                first_run = Some(mode.to_owned());
                continue;
            }
            if arg == "--background" {
                background = true;
                continue;
            }
            let key = arg
                .to_str()
                .ok_or("起動の引数を確認してください。")?
                .to_owned();
            if ![
                "--repo",
                "--python",
                "--port",
                "--db",
                "--data-dir",
                "--characters-root",
            ]
            .contains(&key.as_str())
            {
                return Err("起動の引数を確認してください。".into());
            }
            values.insert(key, iter.next().ok_or("起動の引数に値が必要です。")?);
        }
        let value = |arg: &str, key: &str| values.get(arg).cloned().or_else(|| get(key));
        if first_run.is_some()
            && (values.contains_key("--db") || values.contains_key("--characters-root"))
        {
            return Err("お試しではDB・人物フォルダを指定できません。".into());
        }
        let repo = value("--repo", "CBS_DESKTOP_REPO")
            .map(PathBuf::from)
            .unwrap_or(default_repo);
        let repo = absolute(
            &env::current_dir().map_err(|_| "現在のフォルダを確認できません。")?,
            repo,
        );
        let python = absolute(
            &repo,
            value("--python", "CBS_DESKTOP_PYTHON")
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    installed
                        .map(|dir| dir.join("runtime/pythonw.exe"))
                        .unwrap_or_else(|| ".venv/Scripts/pythonw.exe".into())
                }),
        );
        let port = value("--port", "CBS_DESKTOP_PORT")
            .unwrap_or_else(|| "8765".into())
            .to_string_lossy()
            .parse::<u16>()
            .map_err(|_| "ポート番号を確認してください。")?;
        if port == 0 {
            return Err("ポート番号を確認してください。".into());
        }
        let db = if first_run.is_some() {
            None
        } else {
            value("--db", "CBS_DESKTOP_DB").map(|p| absolute(&repo, p.into()))
        };
        let explicit_data = value("--data-dir", "CBS_DATA_DIR").filter(|p| !p.is_empty());
        let development = repo.join(".git").exists();
        let data_dir = if let Some(p) = explicit_data.clone() {
            absolute(&repo, p.into())
        } else if let Some(p) = &db {
            p.parent().unwrap().to_path_buf()
        } else if development {
            repo.join("data")
        } else {
            get("LOCALAPPDATA")
                .filter(|p| !p.is_empty())
                .map(PathBuf::from)
                .or_else(|| {
                    get("USERPROFILE")
                        .or_else(|| get("HOME"))
                        .map(|p| PathBuf::from(p).join("AppData/Local"))
                })
                .ok_or("ユーザーフォルダを確認できません。")?
                .join("Awalia")
        };
        // An explicit storage directory also isolates partner files.
        let characters = (if first_run.is_some() {
            None
        } else {
            value("--characters-root", "CBS_DESKTOP_CHARACTERS")
        })
        .map(|p| absolute(&repo, p.into()))
        .unwrap_or_else(|| {
            if development && db.is_none() && explicit_data.is_none() {
                repo.join("characters")
            } else {
                data_dir.join("characters")
            }
        });
        Ok(Self {
            repo,
            python,
            port,
            db,
            data_dir,
            characters,
            background: background && first_run.is_none(),
            first_run,
            first_run_source: None,
        })
    }

    pub fn prepare_first_run(&mut self) -> Result<(), String> {
        let Some(mode) = &self.first_run else {
            return Ok(());
        };
        // pythonw has no stdout; use its console sibling with a hidden process.
        let python = if self.python.file_name().is_some_and(|n| n == "pythonw.exe") {
            self.python.with_file_name("python.exe")
        } else {
            self.python.clone()
        };
        let mut command = std::process::Command::new(python);
        command
            .current_dir(&self.repo)
            .args(["-m", "cbs.first_run", "--prepare", mode, "--data-dir"])
            .arg(&self.data_dir);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let output = command
            .output()
            .map_err(|_| "お試しを準備できませんでした。")?;
        if !output.status.success() {
            return Err("お試しを準備できませんでした。".into());
        }
        let directory: PathBuf = serde_json::from_slice::<String>(&output.stdout)
            .map(PathBuf::from)
            .map_err(|_| "お試しの保存先を確認できませんでした。")?;
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0))
            .map_err(|_| "お試しのポートを確保できませんでした。")?;
        self.port = listener
            .local_addr()
            .map_err(|_| "ポートを確認できませんでした。")?
            .port();
        self.first_run_source = Some(self.data_dir.clone());
        self.data_dir = directory;
        self.db = Some(self.data_dir.join("cbs.sqlite3"));
        self.characters = self.data_dir.join("characters");
        Ok(())
    }

    pub fn backend_args(&self) -> Vec<OsString> {
        if let Some(mode) = &self.first_run {
            return vec![
                "-m".into(),
                "cbs.web".into(),
                format!("--first-run={mode}").into(),
                "--first-run-dir".into(),
                self.data_dir.clone().into(),
                "--data-dir".into(),
                self.first_run_source.as_ref().unwrap().clone().into(),
                "--port".into(),
                self.port.to_string().into(),
            ];
        }
        let mut args = vec![
            "-m".into(),
            "cbs.web".into(),
            "--port".into(),
            self.port.to_string().into(),
            "--data-dir".into(),
            self.data_dir.clone().into(),
            "--characters-root".into(),
            self.characters.clone().into(),
        ];
        if let Some(db) = &self.db {
            args.extend([OsString::from("--db"), db.clone().into()]);
        }
        args
    }

    pub fn autostart_args(&self) -> Vec<String> {
        let mut args = vec!["--background".into()];
        for (key, path) in [
            ("--repo", &self.repo),
            ("--python", &self.python),
            ("--data-dir", &self.data_dir),
            ("--characters-root", &self.characters),
        ] {
            args.extend([key.into(), path.to_string_lossy().into_owned()]);
        }
        args.extend(["--port".into(), self.port.to_string()]);
        if let Some(db) = &self.db {
            args.extend(["--db".into(), db.to_string_lossy().into_owned()]);
        }
        args
    }
}

pub fn restart_delay(restarts: usize) -> Option<Duration> {
    (restarts < MAX_RESTARTS).then(|| Duration::from_secs(1 << restarts))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn executable_layout_selects_installed_defaults_and_preserves_overrides() {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../.tmp/r4-options")
            .join(std::process::id().to_string());
        let exe_dir = root.join("installed");
        let dev = root.join("development");
        let local = root.join("local");
        std::fs::create_dir_all(dev.join(".git")).unwrap();
        let get = |key: &str| match key {
            "LOCALAPPDATA" => Some(local.clone().into_os_string()),
            _ => None,
        };
        let before = Options::parse_at(vec![], get, dev.clone(), Some(&exe_dir)).unwrap();
        assert_eq!(before.repo, dev);
        assert_eq!(before.python, dev.join(".venv/Scripts/pythonw.exe"));
        assert_eq!(before.data_dir, dev.join("data"));
        std::fs::create_dir_all(exe_dir.join("app/cbs")).unwrap();
        let installed = Options::parse_at(vec![], get, dev.clone(), Some(&exe_dir)).unwrap();
        assert_eq!(installed.repo, exe_dir.join("app"));
        assert_eq!(installed.python, exe_dir.join("runtime/pythonw.exe"));
        assert_eq!(installed.data_dir, local.join("Awalia"));
        assert_eq!(installed.characters, local.join("Awalia/characters"));
        let get_env = |key: &str| match key {
            "CBS_DESKTOP_REPO" => Some(dev.clone().into_os_string()),
            "CBS_DESKTOP_PYTHON" => Some(root.join("env-python.exe").into_os_string()),
            _ => get(key),
        };
        let specified = Options::parse_at(vec![], get_env, dev.clone(), Some(&exe_dir)).unwrap();
        assert_eq!(specified.repo, dev);
        assert_eq!(specified.python, root.join("env-python.exe"));
        let cli_repo = root.join("cli");
        let specified = Options::parse_at(
            vec![
                "--repo".into(),
                cli_repo.clone().into(),
                "--python".into(),
                "python.exe".into(),
            ],
            get_env,
            dev.clone(),
            Some(&exe_dir),
        )
        .unwrap();
        assert_eq!(specified.repo, cli_repo);
        assert_eq!(specified.python, cli_repo.join("python.exe"));
        assert!(root
            .starts_with(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.tmp/r4-options")));
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn storage_rules_cover_development_packaged_and_explicit_paths() {
        // All files stay in the workspace; never use the user's application data.
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../.tmp/r2-options")
            .join(std::process::id().to_string());
        let repo = root.join("application");
        std::fs::create_dir_all(&repo).unwrap();
        let local = root.join("local");
        let get = |key: &str| match key {
            "LOCALAPPDATA" => Some(local.clone().into_os_string()),
            _ => None,
        };
        let packaged = Options::parse(vec![], get, repo.clone()).unwrap();
        assert_eq!(packaged.data_dir, local.join("Awalia"));
        assert_eq!(packaged.characters, local.join("Awalia/characters"));
        // A .git file also denotes a development worktree.
        std::fs::write(repo.join(".git"), "gitdir: elsewhere").unwrap();
        let dev = Options::parse(vec![], get, repo.clone()).unwrap();
        assert_eq!(dev.data_dir, repo.join("data"));
        assert_eq!(dev.characters, repo.join("characters"));
        let env_data = root.join("environment");
        let get_env = |key: &str| match key {
            "CBS_DATA_DIR" => Some(env_data.clone().into_os_string()),
            _ => get(key),
        };
        let specified = Options::parse(vec![], get_env, repo.clone()).unwrap();
        assert_eq!(specified.data_dir, env_data);
        assert_eq!(specified.characters, env_data.join("characters"));
        let cli = root.join("argument");
        let partner = root.join("partners");
        let specified = Options::parse(
            vec!["--data-dir".into(), cli.clone().into()],
            get_env,
            repo.clone(),
        )
        .unwrap();
        assert_eq!(specified.data_dir, cli);
        assert_eq!(specified.characters, cli.join("characters"));
        let specified = Options::parse(
            vec!["--characters-root".into(), partner.clone().into()],
            get_env,
            repo.clone(),
        )
        .unwrap();
        assert_eq!(specified.characters, partner);
        std::fs::remove_file(repo.join(".git")).unwrap();
        let fallback = Options::parse(
            vec![],
            |key| match key {
                "USERPROFILE" => Some(root.join("home").into_os_string()),
                _ => None,
            },
            repo.clone(),
        )
        .unwrap();
        assert_eq!(fallback.data_dir, root.join("home/AppData/Local/Awalia"));
        // This absolute target is constructed only from the fixed workspace subtree above.
        assert!(root
            .starts_with(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.tmp/r2-options")));
        std::fs::remove_dir_all(root).unwrap();
    }
    fn options(args: &[&str]) -> Options {
        Options::parse(
            args.iter().map(OsString::from).collect(),
            |_| None,
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."),
        )
        .unwrap()
    }
    #[test]
    fn backend_is_python_module_and_check_paths_survive_autostart() {
        let o = options(&[
            "--db",
            ".tmp/check/db.sqlite3",
            "--port",
            "18765",
            "--background",
        ]);
        assert!(o.background);
        assert_eq!(
            &o.backend_args()[..2],
            &[OsString::from("-m"), OsString::from("cbs.web")]
        );
        assert!(o.python.ends_with(".venv/Scripts/pythonw.exe"));
        assert!(o.data_dir.ends_with(".tmp/check"));
        assert!(o.characters.ends_with(".tmp/check/characters"));
        let again = options(
            &o.autostart_args()
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>(),
        );
        assert_eq!(again.db, o.db);
        assert_eq!(again.characters, o.characters);
        assert_eq!(again.port, 18765);
    }
    #[test]
    fn explicit_options_override_environment() {
        let o = Options::parse(
            vec!["--port".into(), "20001".into()],
            |k| (k == "CBS_DESKTOP_PORT").then(|| "20002".into()),
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."),
        )
        .unwrap();
        assert_eq!(o.port, 20001);
        assert!(Options::parse(
            vec!["--port".into(), "0".into()],
            |_| None,
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
        )
        .is_err());
    }
    #[test]
    fn trial_modes_ignore_live_paths_and_pin_backend_restarts() {
        for mode in ["new", "keep", "bare"] {
            let mut o = Options::parse(
                vec![format!("--first-run={mode}").into(), "--background".into()],
                |key| match key {
                    "CBS_DESKTOP_DB" => Some("real.sqlite3".into()),
                    "CBS_DESKTOP_CHARACTERS" => Some("real-characters".into()),
                    _ => None,
                },
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."),
            )
            .unwrap();
            assert!(!o.background);
            assert!(o.db.is_none());
            assert!(o.data_dir.ends_with("data"));
            o.first_run_source = Some(o.data_dir.clone());
            o.data_dir = o.data_dir.join("first-run/test");
            let args = o.backend_args();
            assert!(args.contains(&OsString::from(format!("--first-run={mode}"))));
            assert!(args.contains(&OsString::from("--first-run-dir")));
            assert!(!args.contains(&OsString::from("--db")));
            assert!(!args.contains(&OsString::from("--characters-root")));
            assert_eq!(args, o.backend_args());
        }
        assert_eq!(options(&["--first-run"]).first_run.as_deref(), Some("new"));
        assert!(Options::parse(
            vec!["--first-run=bad".into()],
            |_| None,
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
        )
        .is_err());
    }
    #[test]
    fn restart_budget_never_resets_after_a_successful_start() {
        assert_eq!(
            (0..6).filter_map(restart_delay).collect::<Vec<_>>(),
            vec![
                Duration::from_secs(1),
                Duration::from_secs(2),
                Duration::from_secs(4)
            ]
        );
    }
}
