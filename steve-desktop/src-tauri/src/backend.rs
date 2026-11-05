//! Runs `steve-agent serve` and talks to it. All app traffic goes through here: the server has no
//! CORS by design, and this keeps any configured API key out of the webview.

use std::collections::VecDeque;
use std::env;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Mutex;
use std::time::Duration;

use base64::{Engine, engine::general_purpose::STANDARD};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::{oneshot, watch};

use crate::sse::SseParser;

const START_TIMEOUT: Duration = Duration::from_secs(60);
const LOG_LINES: usize = 30;

#[derive(Clone, Debug, Serialize, PartialEq)]
#[serde(tag = "state", rename_all = "lowercase")]
pub enum Status {
    Starting,
    Ready,
    Failed { message: String },
}

pub struct Paths {
    pub home: PathBuf,
    pub executable: PathBuf,
    /// The directory the agent works in. The CLI never falls back to where it was started.
    pub workspace: Option<PathBuf>,
}

impl Paths {
    /// Finds the CLI: STEVE_AGENT_BIN, then PATH, then where `uv tool install` puts it, then in a
    /// debug build this checkout's own virtual environment. Desktop launchers often have a short PATH.
    pub fn discover() -> Result<Self, String> {
        let home = user_home().ok_or("Could not find your home folder.")?;
        let mut candidates: Vec<PathBuf> = Vec::new();
        if let Some(path) = env::var_os("STEVE_AGENT_BIN") {
            candidates.push(path.into());
        }
        if let Some(path) = env::var_os("PATH") {
            candidates.extend(env::split_paths(&path).map(|dir| dir.join(CLI_NAME)));
        }
        candidates.push(home.join(".local/bin").join(CLI_NAME));
        if cfg!(debug_assertions) {
            candidates.push(
                Path::new(env!("CARGO_MANIFEST_DIR")).join("../../steve-cli/.venv/bin/steve-agent"),
            );
        }
        let executable = candidates.into_iter().find(|path| path.is_file()).ok_or(
            "Could not find the steve-agent command. Install it with `uv tool install ./steve-cli`, \
             or set STEVE_AGENT_BIN to its path.",
        )?;
        Ok(Self {
            home,
            executable,
            workspace: None,
        })
    }

    fn config_file(&self) -> PathBuf {
        self.home.join(".config/steve/config.yaml")
    }
}

#[derive(Deserialize, Default)]
struct Config {
    #[serde(default)]
    server: ServerConfig,
}

#[derive(Deserialize, Default)]
struct ServerConfig {
    api_key: Option<String>,
    max_asset_mb: Option<f64>,
}

const CLI_NAME: &str = if cfg!(windows) { "steve-agent.exe" } else { "steve-agent" };

/// Matches the server's default for `server.max_asset_mb`.
const DEFAULT_MAX_ASSET_MB: f64 = 25.0;

/// One user message: the text, then each attached file inline. Checked here too, because a huge
/// file should fail before it is read into memory and encoded.
async fn build_input(text: &str, attachments: &[PathBuf], max_mb: f64) -> Result<Value, String> {
    let mut content = Vec::new();
    if !text.is_empty() {
        content.push(json!({"type": "input_text", "text": text}));
    }
    for path in attachments {
        let name = path
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_else(|| "file".to_owned());
        let metadata = tokio::fs::metadata(path)
            .await
            .map_err(|e| format!("Could not read {name}: {e}"))?;
        if !metadata.is_file() {
            return Err(format!("{name} is not a file."));
        }
        if metadata.len() as f64 > max_mb * 1024.0 * 1024.0 {
            return Err(format!("{name} is over the {max_mb} MB limit."));
        }
        let bytes = tokio::fs::read(path)
            .await
            .map_err(|e| format!("Could not read {name}: {e}"))?;
        let media_type = mime_guess::from_path(path).first_or_octet_stream();
        let data = format!("data:{media_type};base64,{}", STANDARD.encode(bytes));
        content.push(json!({"type": "input_file", "filename": name, "file_data": data}));
    }
    Ok(json!([{"role": "user", "content": content}]))
}

/// A crash or a kill skips the app's exit handler, so have the kernel stop the server when the app dies.
#[cfg(target_os = "linux")]
fn stop_with_parent(command: &mut Command) {
    // SAFETY: prctl is async-signal-safe, and nothing else happens between fork and exec.
    unsafe {
        command.pre_exec(|| {
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
}

#[cfg(not(target_os = "linux"))]
fn stop_with_parent(_command: &mut Command) {}

fn free_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    Ok(port)
}

/// A running CLI server. Dropping it kills the process.
pub struct Backend {
    base_url: String,
    api_key: Option<String>,
    max_asset_mb: f64,
    http: reqwest::Client,
    status: watch::Receiver<Status>,
    stop: Mutex<Option<oneshot::Sender<()>>>,
}

impl Backend {
    /// Starts the CLI and returns once it answers, or fails with a message fit to show the user.
    pub async fn start(paths: &Paths) -> Result<Self, String> {
        // With no config the CLI would prompt on stdin, which nobody can answer here.
        let config_file = paths.config_file();
        let Ok(config) = std::fs::read_to_string(&config_file) else {
            return Err(format!(
                "Steve is not configured yet. Run `{}` once in a terminal to set the model, then try again.",
                paths.executable.display()
            ));
        };
        let config: Config = serde_norway::from_str(&config)
            .map_err(|e| format!("Could not read {}: {e}", config_file.display()))?;

        let port = free_port()?;
        let mut command = cli_command(&paths.executable);
        if let Some(workspace) = &paths.workspace {
            command.arg("serve").arg("--workspace").arg(workspace);
        } else {
            command.arg("serve");
        }
        command
            .args(["--host", "127.0.0.1", "--port", &port.to_string()])
            .current_dir(&paths.home)
            .env("HOME", &paths.home)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        stop_with_parent(&mut command);
        let mut child = command
            .spawn()
            .map_err(|e| format!("Could not start {}: {e}", paths.executable.display()))?;

        let log = collect_log(&mut child);
        let base_url = format!("http://127.0.0.1:{port}");
        let http = reqwest::Client::new();
        wait_until_ready(&http, &base_url, &mut child, &log).await?;

        let (status_tx, status) = watch::channel(Status::Ready);
        let (stop, stopped) = oneshot::channel();
        tauri::async_runtime::spawn(supervise(child, stopped, status_tx, log));

        Ok(Self {
            base_url,
            api_key: config.server.api_key,
            max_asset_mb: config.server.max_asset_mb.unwrap_or(DEFAULT_MAX_ASSET_MB),
            http,
            status,
            stop: Mutex::new(Some(stop)),
        })
    }

    pub fn status(&self) -> watch::Receiver<Status> {
        self.status.clone()
    }

    pub fn stop(&self) {
        if let Some(stop) = self.stop.lock().unwrap().take() {
            let _ = stop.send(());
        }
    }

    fn authorized(&self, request: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        match &self.api_key {
            Some(key) => request.bearer_auth(key),
            None => request,
        }
    }

    /// Sends a message and passes each event on until the run ends or `cancel` fires. Hanging up
    /// makes the server cancel the run.
    pub async fn send_message(
        &self,
        input: &str,
        attachments: &[PathBuf],
        previous_response_id: Option<&str>,
        mode: &str,
        on_event: impl FnMut(Value),
        cancel: oneshot::Receiver<()>,
    ) -> Result<(), String> {
        let input = build_input(input, attachments, self.max_asset_mb).await?;
        let mut body = json!({"model": "steve-agent", "input": input, "stream": true, "mode": mode});
        if let Some(previous) = previous_response_id {
            body["previous_response_id"] = previous.into();
        }
        let request = self
            .http
            .post(format!("{}/v1/responses", self.base_url))
            .json(&body);
        pump(self.authorized(request), on_event, cancel).await
    }

    /// Follows a response from the start, replaying what already happened. Used to watch subagents.
    pub async fn watch_response(
        &self,
        response_id: &str,
        on_event: impl FnMut(Value),
        cancel: oneshot::Receiver<()>,
    ) -> Result<(), String> {
        let url = format!("{}/v1/responses/{response_id}", self.base_url);
        let request = self.http.get(url).query(&[("stream", "true")]);
        pump(self.authorized(request), on_event, cancel).await
    }

    /// Compacts a conversation, returning the new response to continue from.
    pub async fn compact(&self, response_id: &str) -> Result<Value, String> {
        let url = format!("{}/v1/responses/{response_id}/compact", self.base_url);
        let response = self
            .authorized(self.http.post(url))
            .send()
            .await
            .map_err(connection_error)?;
        if !response.status().is_success() {
            return Err(error_message(response).await);
        }
        response.json().await.map_err(|e| e.to_string())
    }

    pub async fn answer_question(&self, response_id: &str, question_id: &str, answer: Value) -> Result<(), String> {
        let url = format!("{}/v1/responses/{response_id}/questions/{question_id}", self.base_url);
        let response = self.authorized(self.http.post(url)).json(&answer).send().await.map_err(connection_error)?;
        if !response.status().is_success() {
            return Err(error_message(response).await);
        }
        Ok(())
    }

    /// A POST to the CLI's MCP App routes, passing its answer or error along.
    pub async fn mcp_app(&self, route: &str, body: Value) -> Result<Value, String> {
        let url = format!("{}/v1/mcp/apps/{route}", self.base_url);
        let response = self.authorized(self.http.post(url)).json(&body).send().await.map_err(connection_error)?;
        if !response.status().is_success() {
            return Err(error_message(response).await);
        }
        response.json().await.map_err(|e| e.to_string())
    }

    pub async fn conversations(&self) -> Result<Value, String> {
        let url = format!("{}/v1/conversations", self.base_url);
        let response = self.authorized(self.http.get(url)).send().await.map_err(connection_error)?;
        if !response.status().is_success() {
            return Err(error_message(response).await);
        }
        response.json().await.map_err(|e| e.to_string())
    }

    pub async fn conversation(&self, id: &str) -> Result<Value, String> {
        let url = format!("{}/v1/conversations/{id}", self.base_url);
        let response = self.authorized(self.http.get(url)).send().await.map_err(connection_error)?;
        if !response.status().is_success() {
            return Err(error_message(response).await);
        }
        response.json().await.map_err(|e| e.to_string())
    }

    pub async fn delete_conversation(&self, id: &str) -> Result<(), String> {
        let url = format!("{}/v1/conversations/{id}", self.base_url);
        let response = self.authorized(self.http.delete(url)).send().await.map_err(connection_error)?;
        // Already gone is what the caller wanted.
        if response.status().is_success() || response.status() == 404 {
            return Ok(());
        }
        Err(error_message(response).await)
    }

    /// How each MCP server's connection went when the CLI started.
    pub async fn mcp_status(&self) -> Result<Value, String> {
        let url = format!("{}/v1/mcp", self.base_url);
        let response = self.authorized(self.http.get(url)).send().await.map_err(connection_error)?;
        if !response.status().is_success() {
            return Err(error_message(response).await);
        }
        response.json().await.map_err(|e| e.to_string())
    }

    pub async fn delete_response(&self, response_id: &str) -> Result<(), String> {
        let url = format!("{}/v1/responses/{response_id}", self.base_url);
        let response = self
            .authorized(self.http.delete(url))
            .send()
            .await
            .map_err(connection_error)?;
        // Already gone is what the caller wanted, and the server evicts responses by itself.
        if response.status().is_success() || response.status() == 404 {
            return Ok(());
        }
        Err(error_message(response).await)
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        self.stop();
    }
}

fn collect_log(child: &mut Child) -> tokio::sync::watch::Receiver<VecDeque<String>> {
    let (tx, rx) = watch::channel(VecDeque::new());
    if let Some(stderr) = child.stderr.take() {
        tauri::async_runtime::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                tx.send_modify(|log| {
                    log.push_back(line);
                    if log.len() > LOG_LINES {
                        log.pop_front();
                    }
                });
            }
        });
    }
    rx
}

/// The last log line that says something, which for a Python crash is the exception. The full log
/// can hold details that do not belong on screen.
fn exit_message(log: &watch::Receiver<VecDeque<String>>, what: String) -> String {
    let log = log.borrow();
    match log.iter().rev().find(|line| !line.trim().is_empty()) {
        Some(line) => format!("{what} Its last message was: {line}"),
        None => what,
    }
}

async fn wait_until_ready(
    http: &reqwest::Client,
    base_url: &str,
    child: &mut Child,
    log: &watch::Receiver<VecDeque<String>>,
) -> Result<(), String> {
    let health = format!("{base_url}/health");
    let deadline = tokio::time::Instant::now() + START_TIMEOUT;
    loop {
        if let Ok(Some(code)) = child.try_wait() {
            // Give the log reader a moment to catch up with the process's final words.
            tokio::time::sleep(Duration::from_millis(100)).await;
            return Err(exit_message(
                log,
                format!("The steve-agent server exited during startup ({code})."),
            ));
        }
        let probe = http
            .get(&health)
            .timeout(Duration::from_secs(2))
            .send()
            .await;
        if probe.is_ok_and(|response| response.status().is_success()) {
            return Ok(());
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(exit_message(
                log,
                "The steve-agent server did not become ready in time.".into(),
            ));
        }
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
}

async fn supervise(
    mut child: Child,
    stopped: oneshot::Receiver<()>,
    status: watch::Sender<Status>,
    log: watch::Receiver<VecDeque<String>>,
) {
    tokio::select! {
        code = child.wait() => {
            let code = code.map(|c| c.to_string()).unwrap_or_else(|e| e.to_string());
            let message = exit_message(&log, format!("The steve-agent server stopped ({code})."));
            let _ = status.send(Status::Failed { message });
        }
        _ = stopped => {
            let _ = child.kill().await;
        }
    }
}

fn connection_error(error: reqwest::Error) -> String {
    format!("Could not reach the steve-agent server: {error}")
}

async fn error_message(response: reqwest::Response) -> String {
    let status = response.status();
    let body: Option<Value> = response.json().await.ok();
    body.as_ref()
        .and_then(|b| b.pointer("/error/message"))
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| format!("The server answered with {status}."))
}

async fn pump(
    request: reqwest::RequestBuilder,
    mut on_event: impl FnMut(Value),
    mut cancel: oneshot::Receiver<()>,
) -> Result<(), String> {
    let response = tokio::select! {
        response = request.send() => response.map_err(connection_error)?,
        _ = &mut cancel => return Ok(()),
    };
    if !response.status().is_success() {
        return Err(error_message(response).await);
    }
    let mut parser = SseParser::default();
    let mut stream = response.bytes_stream();
    loop {
        let chunk = tokio::select! {
            chunk = stream.next() => chunk,
            _ = &mut cancel => return Ok(()),
        };
        let Some(chunk) = chunk else {
            return Ok(());
        };
        let chunk = chunk.map_err(|e| format!("The connection to the server was lost: {e}"))?;
        for data in parser.feed(&chunk) {
            // A malformed event should not end the whole stream.
            if let Ok(event) = serde_json::from_str(&data) {
                on_event(event);
            }
        }
    }
}

/// The person's home folder: Windows has no HOME.
pub fn user_home() -> Option<PathBuf> {
    home_from(env::var_os("HOME"), env::var_os("USERPROFILE"))
}

fn home_from(home: Option<std::ffi::OsString>, profile: Option<std::ffi::OsString>) -> Option<PathBuf> {
    home.filter(|path| !path.is_empty()).or(profile.filter(|path| !path.is_empty())).map(PathBuf::from)
}

/// A command for the CLI. On Windows, without this flag, it would open a console window of its own.
pub fn cli_command(executable: &Path) -> Command {
    #[allow(unused_mut)]
    let mut command = Command::new(executable);
    #[cfg(windows)]
    command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    command
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn home_falls_back_to_the_windows_profile() {
        let found = home_from(None, Some("C:\\Users\\me".into()));
        assert_eq!(found, Some(PathBuf::from("C:\\Users\\me")));
        assert_eq!(home_from(Some("/home/me".into()), Some("other".into())), Some(PathBuf::from("/home/me")));
        assert_eq!(home_from(Some("".into()), None), None);
    }
}
