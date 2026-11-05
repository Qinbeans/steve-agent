mod addons;
mod backend;
mod sse;

pub use backend::{Backend, Paths, Status};

use std::collections::HashMap;
use std::fs;
use std::hash::{BuildHasher, Hasher};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tauri::ipc::Channel;
use tauri::webview::PageLoadEvent;
use tauri::menu::{Menu, MenuBuilder, MenuItemBuilder, SubmenuBuilder};
use tauri::{AppHandle, Emitter, Manager, RunEvent, State};
use tokio::sync::{RwLock, oneshot};

const STATUS_EVENT: &str = "backend-status";
const MAX_RECENT: usize = 8;
const SHOW_WINDOW_ANYWAY: std::time::Duration = std::time::Duration::from_secs(5);

#[derive(Serialize, Deserialize)]
struct Settings {
    workspace: Option<String>,
    #[serde(default)]
    recent: Vec<String>,
}

#[derive(Serialize)]
struct WorkspaceState {
    path: Option<String>,
    recent: Vec<String>,
}

#[derive(Default)]
struct AppState {
    backend: RwLock<Option<Arc<Backend>>>,
    start: tokio::sync::Mutex<()>,
    status: Mutex<Option<Status>>,
    streams: Mutex<HashMap<String, oneshot::Sender<()>>>,
    /// Open MCP App pages by token: their HTML and Content-Security-Policy.
    app_pages: Mutex<HashMap<String, (String, String)>>,
}

#[cfg(windows)]
const APP_ORIGIN: &str = "http://mcp-app.localhost";
#[cfg(not(windows))]
const APP_ORIGIN: &str = "mcp-app://localhost";

fn publish_status(app: &AppHandle, status: Status) {
    let state = app.state::<AppState>();
    *state.status.lock().unwrap() = Some(status.clone());
    let _ = app.emit(STATUS_EVENT, status);
}

fn settings_file(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(app.path().app_config_dir().map_err(|e| e.to_string())?.join("settings.json"))
}

/// None until a folder has been chosen for the first time.
fn read_settings(app: &AppHandle) -> Option<Settings> {
    let text = fs::read_to_string(settings_file(app).ok()?).ok()?;
    serde_json::from_str(&text).ok()
}

#[tauri::command]
fn get_workspace(app: AppHandle) -> WorkspaceState {
    let Some(settings) = read_settings(&app) else {
        return WorkspaceState { path: None, recent: Vec::new() };
    };
    // The current folder is listed even if it was saved before recent folders existed.
    let recent = remember(settings.recent, settings.workspace.as_deref())
        .into_iter()
        .filter(|path| Path::new(path).is_dir())
        .collect();
    WorkspaceState { path: settings.workspace, recent }
}

async fn launch(app: &AppHandle, state: &AppState) -> Status {
    publish_status(app, Status::Starting);
    let started = match Paths::discover() {
        Ok(mut paths) => {
            paths.workspace = read_settings(app).and_then(|s| s.workspace).map(PathBuf::from);
            Backend::start(&paths).await
        }
        Err(message) => Err(message),
    };
    let status = match started {
        Ok(backend) => {
            let mut changes = backend.status();
            let watcher = app.clone();
            tauri::async_runtime::spawn(async move {
                while changes.changed().await.is_ok() {
                    let status = changes.borrow_and_update().clone();
                    publish_status(&watcher, status);
                }
            });
            *state.backend.write().await = Some(Arc::new(backend));
            Status::Ready
        }
        Err(message) => Status::Failed { message },
    };
    publish_status(app, status.clone());
    status
}

/// Starts the CLI if it is not running. Safe to call again, which is how the UI retries.
#[tauri::command]
async fn start_backend(app: AppHandle, state: State<'_, AppState>) -> Result<Status, String> {
    let _starting = state.start.lock().await;
    if let Some(Status::Ready) = *state.status.lock().unwrap() {
        return Ok(Status::Ready);
    }
    Ok(launch(&app, &state).await)
}

/// Remembers the directory and restarts the CLI in it, as its agent is built for one directory.
#[tauri::command]
async fn set_workspace(app: AppHandle, state: State<'_, AppState>, path: Option<String>) -> Result<Status, String> {
    let workspace = match path.as_deref().map(str::trim).filter(|p| !p.is_empty()) {
        Some(path) => {
            let resolved = fs::canonicalize(expand_home(path)).map_err(|_| format!("{path} does not exist."))?;
            if !resolved.is_dir() {
                return Err(format!("{path} is not a folder."));
            }
            Some(resolved.to_string_lossy().into_owned())
        }
        None => None,
    };
    let file = settings_file(&app)?;
    if let Some(parent) = file.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let known = read_settings(&app).map(|settings| settings.recent).unwrap_or_default();
    let recent = remember(known, workspace.as_deref());
    let text = serde_json::to_string_pretty(&Settings { workspace, recent }).map_err(|e| e.to_string())?;
    fs::write(&file, text).map_err(|e| format!("Could not save the folder: {e}"))?;
    Ok(restart(&app, &state).await)
}

/// A reply in progress does not survive this. Saved conversations do.
async fn restart(app: &AppHandle, state: &State<'_, AppState>) -> Status {
    let _starting = state.start.lock().await;
    if let Some(old) = state.backend.write().await.take() {
        old.stop();
    }
    launch(app, state).await
}

/// The servers in mcp.json, with how each connection went if the agent is running.
#[tauri::command]
async fn get_mcp_servers(state: State<'_, AppState>) -> Result<Value, String> {
    let entries = addons::list_mcp(&addons::steve_home()?)?;
    let live = match running(&state).await {
        Ok(backend) => backend.mcp_status().await.unwrap_or_default(),
        Err(_) => Value::Null,
    };
    let live = live.get("servers").and_then(Value::as_array).cloned().unwrap_or_default();
    let status_of = |name: &str| live.iter().find(|server| server["name"] == name);
    let mut servers: Vec<Value> = entries
        .iter()
        .map(|entry| {
            let status = status_of(&entry.name);
            json!({
                "name": entry.name,
                "kind": entry.kind,
                "target": entry.target,
                "auth": entry.auth,
                "status": status.map_or(json!("unknown"), |s| s["status"].clone()),
                "tools": status.map_or(json!([]), |s| s["tools"].clone()),
                "error": status.map_or(Value::Null, |s| s["error"].clone()),
            })
        })
        .collect();
    // A broken mcp.json has no entries, and the CLI reports it under the file's name.
    if let Some(problem) = status_of("mcp.json") {
        servers.push(json!({"name": "mcp.json", "kind": "unknown", "target": "", "auth": "none",
            "status": "failed", "tools": [], "error": problem["error"]}));
    }
    Ok(Value::Array(servers))
}

#[tauri::command]
async fn add_mcp_server(app: AppHandle, state: State<'_, AppState>, server: addons::NewMcpServer) -> Result<Status, String> {
    addons::add_mcp(&addons::steve_home()?, server)?;
    Ok(restart(&app, &state).await)
}

#[tauri::command]
async fn remove_mcp_server(app: AppHandle, state: State<'_, AppState>, name: String) -> Result<Status, String> {
    addons::remove_mcp(&addons::steve_home()?, &name)?;
    Ok(restart(&app, &state).await)
}

#[tauri::command]
fn get_agent_settings() -> Result<addons::AgentSettings, String> {
    addons::read_settings(&addons::steve_home()?)
}

/// The agent is built with its settings, so they take effect by restarting it.
#[tauri::command]
async fn set_agent_settings(app: AppHandle, state: State<'_, AppState>, settings: addons::AgentSettings) -> Result<Status, String> {
    addons::write_settings(&addons::steve_home()?, settings)?;
    Ok(restart(&app, &state).await)
}

/// Whether the browser for the web tools starts. Says why not, if it does not.
#[tauri::command]
async fn browser_status() -> Result<(), String> {
    run_web_command("status").await
}

/// Downloads the browser, unless Settings names one of the person's own.
#[tauri::command]
async fn setup_browser() -> Result<(), String> {
    run_web_command("setup").await
}

async fn run_web_command(command: &str) -> Result<(), String> {
    let paths = Paths::discover()?;
    let output = backend::cli_command(&paths.executable)
        .args(["web", command])
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true)
        .output()
        .await
        .map_err(|e| format!("Could not run steve-agent: {e}"))?;
    if output.status.success() {
        return Ok(());
    }
    let message = String::from_utf8_lossy(&output.stderr);
    Err(message.lines().last().unwrap_or("The browser could not start.").to_owned())
}

/// Runs `steve-agent mcp login`, which opens the browser, then restarts the agent to connect.
#[tauri::command]
async fn mcp_login(app: AppHandle, state: State<'_, AppState>, name: String) -> Result<Status, String> {
    let paths = Paths::discover()?;
    let output = backend::cli_command(&paths.executable)
        .args(["mcp", "login", &name])
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true)
        .output()
        .await
        .map_err(|e| format!("Could not run steve-agent: {e}"))?;
    if !output.status.success() {
        let message = String::from_utf8_lossy(&output.stderr);
        return Err(message.lines().last().unwrap_or("Signing in failed.").to_owned());
    }
    Ok(restart(&app, &state).await)
}

/// Fetches an app's page and serves it from the mcp-app scheme, with its own policy. It is a
/// separate origin, not srcdoc, because a srcdoc frame would inherit this window's policy.
#[tauri::command]
async fn mcp_app_open(state: State<'_, AppState>, server: String, uri: String) -> Result<Value, String> {
    let page = running(&state).await?.mcp_app("page", json!({"server": server, "uri": uri})).await?;
    let html = page["html"].as_str().ok_or("The app's page is empty.")?.to_owned();
    let csp = addons::app_csp(page.get("csp").filter(|csp| !csp.is_null()));
    let token = format!("{:016x}", std::collections::hash_map::RandomState::new().build_hasher().finish());
    state.app_pages.lock().unwrap().insert(token.clone(), (html, csp));
    Ok(json!({"url": format!("{APP_ORIGIN}/{token}"), "token": token, "prefersBorder": page["prefers_border"]}))
}

#[tauri::command]
fn mcp_app_close(state: State<'_, AppState>, token: String) {
    state.app_pages.lock().unwrap().remove(&token);
}

#[tauri::command]
async fn mcp_app_call(state: State<'_, AppState>, server: String, name: String, arguments: Value) -> Result<Value, String> {
    running(&state).await?.mcp_app("call", json!({"server": server, "name": name, "arguments": arguments})).await
}

#[tauri::command]
async fn mcp_app_read(state: State<'_, AppState>, server: String, uri: String) -> Result<Value, String> {
    running(&state).await?.mcp_app("resource", json!({"server": server, "uri": uri})).await
}

fn app_page_response(state: &AppState, path: &str) -> tauri::http::Response<Vec<u8>> {
    let page = state.app_pages.lock().unwrap().get(path.trim_start_matches('/')).cloned();
    let builder = tauri::http::Response::builder();
    match page {
        Some((html, csp)) => builder
            .header("Content-Type", "text/html; charset=utf-8")
            .header("Content-Security-Policy", csp)
            .header("Referrer-Policy", "no-referrer")
            .body(html.into_bytes()),
        None => builder.status(404).body(Vec::new()),
    }
    .expect("the headers are valid")
}

#[tauri::command]
async fn answer_question(state: State<'_, AppState>, response_id: String, question_id: String, answer: Value) -> Result<(), String> {
    running(&state).await?.answer_question(&response_id, &question_id, answer).await
}

#[tauri::command]
async fn list_conversations(state: State<'_, AppState>) -> Result<Value, String> {
    running(&state).await?.conversations().await
}

#[tauri::command]
async fn get_conversation(state: State<'_, AppState>, id: String) -> Result<Value, String> {
    running(&state).await?.conversation(&id).await
}

#[tauri::command]
async fn delete_conversation(state: State<'_, AppState>, id: String) -> Result<(), String> {
    running(&state).await?.delete_conversation(&id).await
}

#[tauri::command]
fn save_feedback(feedback: Value) -> Result<(), String> {
    addons::save_feedback(&addons::steve_home()?, &feedback)
}

#[tauri::command]
fn get_skills(app: AppHandle) -> Result<Vec<addons::Skill>, String> {
    let workspace = read_settings(&app).and_then(|settings| settings.workspace).map(PathBuf::from);
    Ok(addons::list_skills(&addons::steve_home()?, workspace.as_deref()))
}

/// The agent reloads skills on every run, so this needs no restart.
#[tauri::command]
fn add_skill(path: String) -> Result<String, String> {
    addons::add_skill(&addons::steve_home()?, Path::new(&path))
}

#[tauri::command]
fn remove_skill(name: String) -> Result<(), String> {
    addons::remove_skill(&addons::steve_home()?, &name)
}

/// Puts the folder first in the recent list, which keeps its last few entries.
fn remember(mut recent: Vec<String>, folder: Option<&str>) -> Vec<String> {
    if let Some(folder) = folder {
        recent.retain(|path| path != folder);
        recent.insert(0, folder.to_string());
        recent.truncate(MAX_RECENT);
    }
    recent
}

fn expand_home(path: &str) -> PathBuf {
    match (path.strip_prefix("~/"), backend::user_home()) {
        (Some(rest), Some(home)) => PathBuf::from(home).join(rest),
        _ if path == "~" => backend::user_home().map(PathBuf::from).unwrap_or_else(|| path.into()),
        _ => path.into(),
    }
}

async fn running(state: &State<'_, AppState>) -> Result<Arc<Backend>, String> {
    state
        .backend
        .read()
        .await
        .clone()
        .ok_or_else(|| "The steve-agent server is not running.".to_owned())
}

fn register(state: &State<'_, AppState>, stream_id: String) -> oneshot::Receiver<()> {
    let (cancel, cancelled) = oneshot::channel();
    state.streams.lock().unwrap().insert(stream_id, cancel);
    cancelled
}

#[tauri::command]
async fn send_message(
    state: State<'_, AppState>,
    input: String,
    attachments: Vec<String>,
    previous_response_id: Option<String>,
    stream_id: String,
    mode: String,
    on_event: Channel<Value>,
) -> Result<(), String> {
    let backend = running(&state).await?;
    let cancelled = register(&state, stream_id.clone());
    let result = backend
        .send_message(
            &input,
            &attachments.iter().map(PathBuf::from).collect::<Vec<_>>(),
            previous_response_id.as_deref(),
            &mode,
            |event| {
                let _ = on_event.send(event);
            },
            cancelled,
        )
        .await;
    state.streams.lock().unwrap().remove(&stream_id);
    result
}

#[tauri::command]
async fn watch_response(
    state: State<'_, AppState>,
    response_id: String,
    stream_id: String,
    on_event: Channel<Value>,
) -> Result<(), String> {
    let backend = running(&state).await?;
    let cancelled = register(&state, stream_id.clone());
    let result = backend
        .watch_response(
            &response_id,
            |event| {
                let _ = on_event.send(event);
            },
            cancelled,
        )
        .await;
    state.streams.lock().unwrap().remove(&stream_id);
    result
}

#[tauri::command]
fn cancel_stream(state: State<'_, AppState>, stream_id: String) {
    if let Some(cancel) = state.streams.lock().unwrap().remove(&stream_id) {
        let _ = cancel.send(());
    }
}

#[tauri::command]
async fn compact_response(state: State<'_, AppState>, response_id: String) -> Result<Value, String> {
    running(&state).await?.compact(&response_id).await
}

#[tauri::command]
async fn delete_response(state: State<'_, AppState>, response_id: String) -> Result<(), String> {
    running(&state).await?.delete_response(&response_id).await
}

/// The ids the webview acts on start with this. The rest are the system's own items.
const MENU_PREFIX: &str = "steve.";

/// Ctrl+B has no accelerator here because the sidebar already handles it in the webview.
fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let item = |id: &str, label: &str, shortcut: Option<&str>| {
        let builder = MenuItemBuilder::with_id(format!("{MENU_PREFIX}{id}"), label);
        match shortcut {
            Some(shortcut) => builder.accelerator(shortcut).build(app),
            None => builder.build(app),
        }
    };
    let file = SubmenuBuilder::new(app, "File")
        .item(&item("new-chat", "New Conversation", Some("CmdOrCtrl+N"))?)
        .item(&item("open-folder", "Open Folder…", Some("CmdOrCtrl+O"))?)
        .item(&item("attach", "Attach Files…", Some("CmdOrCtrl+Shift+A"))?)
        .separator()
        .item(&item("settings", "Settings…", Some("CmdOrCtrl+,"))?)
        .separator()
        .close_window()
        .quit()
        .build()?;
    let edit = SubmenuBuilder::new(app, "Edit").undo().redo().separator().cut().copy().paste().select_all().build()?;
    let view = SubmenuBuilder::new(app, "View")
        .item(&item("toggle-sidebar", "Toggle Sidebar", None)?)
        .item(&item("toggle-theme", "Toggle Theme", None)?)
        .build()?;
    let tools = SubmenuBuilder::new(app, "Tools")
        .item(&item("compact", "Compact Conversation", None)?)
        .separator()
        .item(&item("add-mcp", "Add MCP Server…", None)?)
        .item(&item("add-skill", "Add Skill from a Folder…", None)?)
        .build()?;
    let help = SubmenuBuilder::new(app, "Help").about(None).build()?;
    let menu = MenuBuilder::new(app);
    // On macOS the first menu is always named after the app, so File needs one in front of it.
    #[cfg(target_os = "macos")]
    let menu = menu.item(&SubmenuBuilder::new(app, "Steve").about(None).separator().hide().hide_others().separator().quit().build()?);
    menu.item(&file).item(&edit).item(&view).item(&tools).item(&help).build()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .register_uri_scheme_protocol("mcp-app", |context, request| {
            app_page_response(&context.app_handle().state::<AppState>(), request.uri().path())
        })
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .manage(AppState::default())
        // Hidden until the page has its splash to show, so the window never appears blank.
        .on_page_load(|webview, payload| {
            if payload.event() == PageLoadEvent::Finished {
                let _ = webview.window().show();
            }
        })
        .setup(|app| {
            // In case the page never finishes loading: a window with a problem beats no window.
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(SHOW_WINDOW_ANYWAY).await;
                if let Some(window) = handle.get_webview_window("main") {
                    let _ = window.show();
                }
            });
            Ok(())
        })
        .menu(build_menu)
        .on_menu_event(|app, event| {
            if let Some(id) = event.id().as_ref().strip_prefix(MENU_PREFIX) {
                let _ = app.emit("menu", id);
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_workspace,
            set_workspace,
            get_mcp_servers,
            add_mcp_server,
            remove_mcp_server,
            mcp_login,
            get_agent_settings,
            set_agent_settings,
            browser_status,
            setup_browser,
            mcp_app_open,
            mcp_app_close,
            mcp_app_call,
            mcp_app_read,
            answer_question,
            list_conversations,
            get_conversation,
            delete_conversation,
            save_feedback,
            get_skills,
            add_skill,
            remove_skill,
            start_backend,
            compact_response,
            send_message,
            watch_response,
            cancel_stream,
            delete_response
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                let state = app.state::<AppState>();
                if let Some(backend) = state.backend.blocking_write().take() {
                    backend.stop();
                }
                // Let the supervisor task deliver the kill before the runtime goes away.
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recent_folders_are_newest_first_without_repeats_and_capped() {
        let recent = remember(vec!["/a".into(), "/b".into()], Some("/b"));
        assert_eq!(recent, ["/b", "/a"]);
        assert_eq!(remember(recent.clone(), None), recent, "no folder leaves the list alone");

        let many = (0..MAX_RECENT).map(|n| format!("/{n}")).collect();
        let capped = remember(many, Some("/new"));
        assert_eq!(capped.len(), MAX_RECENT);
        assert_eq!(capped[0], "/new");
    }

    #[test]
    fn settings_saved_before_recents_existed_still_load() {
        let settings: Settings = serde_json::from_str(r#"{"workspace":"/a"}"#).unwrap();
        assert_eq!(settings.workspace.as_deref(), Some("/a"));
        assert!(settings.recent.is_empty());
    }
}
