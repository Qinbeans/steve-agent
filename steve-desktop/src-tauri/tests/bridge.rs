//! The bridge against the real CLI, with a fake model behind it. Needs `uv sync` in steve-cli.

use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};

use serde_json::Value;
use steve_desktop_lib::{Backend, Paths};
use tokio::sync::oneshot;

fn cli_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../steve-cli")
}

/// The Python test suite's fake Responses upstream, in its own process.
struct Upstream {
    process: Child,
    base_url: String,
}

impl Upstream {
    fn start() -> Self {
        let script = "import sys, time; sys.path.insert(0, 'tests'); from fake_upstream import FakeUpstream; \
                      u = FakeUpstream(); print(u.base_url, flush=True); time.sleep(600)";
        let mut process = Command::new(cli_dir().join(".venv/bin/python"))
            .args(["-c", script])
            .current_dir(cli_dir())
            .stdout(Stdio::piped())
            .spawn()
            .expect("run `uv sync` in steve-cli first");
        let mut line = String::new();
        BufReader::new(process.stdout.take().unwrap())
            .read_line(&mut line)
            .unwrap();
        Self { process, base_url: line.trim().to_owned() }
    }
}

impl Drop for Upstream {
    fn drop(&mut self) {
        let _ = self.process.kill();
    }
}

struct TempHome(PathBuf);

impl TempHome {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("steve-bridge-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        Self(dir)
    }

    fn configure(&self, base_url: &str, server: &str) {
        fs::create_dir_all(self.0.join(".config/steve/agents/memories")).unwrap();
        fs::create_dir_all(self.0.join(".config/steve/agents/skills")).unwrap();
        fs::write(self.0.join(".config/steve/agents/AGENTS.md"), "").unwrap();
        fs::write(self.0.join(".config/steve/agents/memories/MEMORY.md"), "").unwrap();
        fs::write(
            self.0.join(".config/steve/config.yaml"),
            format!("model:\n  model: fake\n  base_url: {base_url}\n  api_key: k\n{server}"),
        )
        .unwrap();
    }

    fn paths(&self) -> Paths {
        Paths {
            home: self.0.clone(),
            executable: cli_dir().join(".venv/bin/steve-agent"),
            workspace: None,
        }
    }
}

impl Drop for TempHome {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

async fn send(backend: &Backend, input: &str, previous: Option<&str>) -> Vec<Value> {
    let mut events = Vec::new();
    let (_keep, cancel) = oneshot::channel();
    backend
        .send_message(input, &[], previous, "auto", |event| events.push(event), cancel)
        .await
        .unwrap();
    events
}

fn terminal(events: &[Value]) -> &Value {
    events.last().unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn runs_the_cli_and_streams_a_conversation() {
    let upstream = Upstream::start();
    let home = TempHome::new("stream");
    home.configure(&upstream.base_url, "");
    let backend = Backend::start(&home.paths()).await.unwrap();

    let events = send(&backend, "hello there", None).await;
    assert_eq!(events[0]["type"], "response.created");
    assert!(events.iter().any(|e| e["type"] == "response.output_text.delta"));
    let last = terminal(&events);
    assert_eq!(last["type"], "response.completed");
    let id = last["response"]["id"].as_str().unwrap();

    let next = send(&backend, "and again", Some(id)).await;
    assert_eq!(terminal(&next)["response"]["previous_response_id"], id);

    // Watching replays the same response.
    let mut watched = Vec::new();
    let (_keep, cancel) = oneshot::channel();
    backend.watch_response(id, |event| watched.push(event), cancel).await.unwrap();
    assert_eq!(terminal(&watched)["response"]["id"], id);

    backend.delete_response(id).await.unwrap();
    backend.delete_response(id).await.unwrap();
    let mut gone = Vec::new();
    let (_keep, cancel) = oneshot::channel();
    let error = backend.watch_response(id, |event| gone.push(event), cancel).await.unwrap_err();
    assert!(error.contains("was not found"), "{error}");
}

#[tokio::test(flavor = "multi_thread")]
async fn sends_the_configured_api_key() {
    let upstream = Upstream::start();
    let home = TempHome::new("key");
    home.configure(&upstream.base_url, "server:\n  api_key: secret-key\n");
    let backend = Backend::start(&home.paths()).await.unwrap();
    assert_eq!(terminal(&send(&backend, "hi", None).await)["type"], "response.completed");
}

#[tokio::test(flavor = "multi_thread")]
async fn cancelling_stops_at_once() {
    let upstream = Upstream::start();
    let home = TempHome::new("cancel");
    home.configure(&upstream.base_url, "");
    let backend = Backend::start(&home.paths()).await.unwrap();
    let (cancel, cancelled) = oneshot::channel();
    cancel.send(()).unwrap();
    let mut events = Vec::new();
    backend.send_message("hi", &[], None, "auto", |event| events.push(event), cancelled).await.unwrap();
    assert!(events.is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn stopping_kills_the_server_and_reports_nothing_as_a_crash() {
    let upstream = Upstream::start();
    let home = TempHome::new("stop");
    home.configure(&upstream.base_url, "");
    let backend = Backend::start(&home.paths()).await.unwrap();
    backend.stop();
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    let (_keep, cancel) = oneshot::channel();
    let error = backend.send_message("hi", &[], None, "auto", |_| {}, cancel).await.unwrap_err();
    assert!(error.contains("Could not reach"), "{error}");
}

#[tokio::test(flavor = "multi_thread")]
async fn missing_config_explains_what_to_do_instead_of_hanging() {
    let home = TempHome::new("noconfig");
    let error = Backend::start(&home.paths()).await.err().unwrap();
    assert!(error.contains("not configured"), "{error}");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_cli_that_dies_on_startup_reports_why() {
    let home = TempHome::new("crash");
    home.configure("http://127.0.0.1:9", "server:\n  port: not-a-number\n");
    let error = Backend::start(&home.paths()).await.err().unwrap();
    assert!(error.contains("exited during startup"), "{error}");
    assert!(error.contains("Its last message was"), "{error}");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_missing_executable_is_reported() {
    let home = TempHome::new("missing");
    home.configure("http://127.0.0.1:9", "");
    let mut paths = home.paths();
    paths.executable = home.0.join("nope");
    let error = Backend::start(&paths).await.err().unwrap();
    assert!(error.contains("Could not start"), "{error}");
}

#[tokio::test(flavor = "multi_thread")]
async fn attachments_reach_the_agent_and_limits_apply() {
    let upstream = Upstream::start();
    let home = TempHome::new("assets");
    home.configure(&upstream.base_url, "server:\n  max_asset_mb: 0.01\n");
    let notes = home.0.join("notes.txt");
    fs::write(&notes, "hello").unwrap();
    let big = home.0.join("big.bin");
    fs::write(&big, vec![0u8; 20_000]).unwrap();
    let backend = Backend::start(&home.paths()).await.unwrap();

    let mut events = Vec::new();
    let (_keep, cancel) = oneshot::channel();
    backend
        .send_message("look", &[notes], None, "auto", |event| events.push(event), cancel)
        .await
        .unwrap();
    assert_eq!(terminal(&events)["type"], "response.completed");
    let sent = upstream_text(&events);
    assert!(sent.contains("notes.txt") && sent.contains("/artifacts/uploads/"), "{sent}");

    for (path, expected) in [
        (big, "over the 0.01 MB limit"),
        (home.0.join("missing.txt"), "Could not read"),
        (home.0.clone(), "not a file"),
    ] {
        let (_keep, cancel) = oneshot::channel();
        let error = backend.send_message("x", &[path], None, "auto", |_| {}, cancel).await.unwrap_err();
        assert!(error.contains(expected), "{error}");
    }
}

/// The fake model echoes the last user message, so this is what it was sent.
fn upstream_text(events: &[Value]) -> String {
    terminal(events)["response"]["output"].to_string()
}

#[tokio::test(flavor = "multi_thread")]
async fn runs_the_cli_in_the_chosen_workspace_only() {
    let upstream = Upstream::start();
    let home = TempHome::new("workspace");
    home.configure(&upstream.base_url, "");
    let workspace = home.0.join("project");
    fs::create_dir_all(&workspace).unwrap();
    let mut paths = home.paths();
    paths.workspace = Some(workspace);
    Backend::start(&paths).await.unwrap();

    let mut bad = home.paths();
    bad.workspace = Some(home.0.join("missing"));
    let error = Backend::start(&bad).await.err().unwrap();
    assert!(error.contains("not a directory"), "{error}");
}

#[tokio::test(flavor = "multi_thread")]
async fn compacts_a_conversation() {
    let upstream = Upstream::start();
    let home = TempHome::new("compact");
    home.configure(&upstream.base_url, "");
    let backend = Backend::start(&home.paths()).await.unwrap();
    let first = send(&backend, "hello", None).await;
    let id = terminal(&first)["response"]["id"].as_str().unwrap().to_owned();
    let compacted = backend.compact(&id).await.unwrap();
    assert_eq!(compacted["previous_response_id"], id.as_str());
    assert!(backend.compact("resp_missing").await.unwrap_err().contains("was not found"));
}
