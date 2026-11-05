//! MCP servers in mcp.json, and skills in the agent's skills folder. No Tauri here, so tests can run
//! it against a temporary directory. Nothing returned from here holds a secret: tokens, headers,
//! environment variables and arguments go in and never come back out to the webview.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

/// Where the CLI keeps its files.
pub fn steve_home() -> Result<PathBuf, String> {
    let home = crate::backend::user_home().ok_or("Could not find your home folder.")?;
    Ok(home.join(".config/steve"))
}

#[derive(Serialize, Debug, PartialEq)]
pub struct McpEntry {
    pub name: String,
    pub kind: &'static str,
    /// The command, or the URL without its query. Arguments and queries can hold secrets.
    pub target: String,
    pub auth: &'static str,
}

#[derive(Deserialize, Default)]
#[serde(default, rename_all = "camelCase")]
pub struct NewMcpServer {
    pub name: String,
    pub command: Option<String>,
    pub args: Vec<String>,
    pub env: Map<String, Value>,
    pub url: Option<String>,
    pub bearer_token: Option<String>,
    pub oauth: bool,
}

fn valid_mcp_name(name: &str) -> bool {
    !name.is_empty() && name.len() <= 64 && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn read_mcp(home: &Path) -> Result<Map<String, Value>, String> {
    match fs::read_to_string(home.join("mcp.json")) {
        Ok(text) => match serde_json::from_str(&text) {
            Ok(Value::Object(map)) => Ok(map),
            _ => Err("mcp.json is not valid. Fix or remove it first.".to_owned()),
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Map::new()),
        Err(error) => Err(format!("Could not read mcp.json: {error}")),
    }
}

/// Only this user can read it, since it can hold tokens. Written whole, so a crash cannot truncate it.
fn write_private(path: &Path, text: &str) -> Result<(), String> {
    let describe = |e: std::io::Error| format!("Could not save {}: {e}", path.display());
    let parent = path.parent().ok_or("The file has no folder.")?;
    fs::create_dir_all(parent).map_err(describe)?;
    let temporary = path.with_extension("tmp");
    let mut options = fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    let mut file = options.open(&temporary).map_err(describe)?;
    file.write_all(text.as_bytes()).map_err(describe)?;
    fs::rename(&temporary, path).map_err(describe)
}

fn write_mcp(home: &Path, data: &Map<String, Value>) -> Result<(), String> {
    let text = serde_json::to_string_pretty(data).map_err(|e| e.to_string())?;
    write_private(&home.join("mcp.json"), &text)
}

pub fn list_mcp(home: &Path) -> Result<Vec<McpEntry>, String> {
    let data = read_mcp(home)?;
    let Some(Value::Object(servers)) = data.get("mcpServers") else {
        return Ok(Vec::new());
    };
    Ok(servers
        .iter()
        .map(|(name, spec)| {
            let text = |key: &str| spec.get(key).and_then(Value::as_str);
            let (kind, target) = match (text("command"), text("url")) {
                (Some(command), _) => ("command", command.to_owned()),
                (_, Some(url)) => ("url", url.split(['?', '#']).next().unwrap_or(url).to_owned()),
                _ => ("unknown", String::new()),
            };
            let auth = if text("auth") == Some("oauth") {
                "oauth"
            } else if spec.get("headers").is_some() {
                "bearer"
            } else {
                "none"
            };
            McpEntry { name: name.clone(), kind, target, auth }
        })
        .collect())
}

pub fn add_mcp(home: &Path, server: NewMcpServer) -> Result<(), String> {
    let name = server.name.trim();
    if !valid_mcp_name(name) {
        return Err("The name can only use letters, numbers, _ and -.".to_owned());
    }
    let mut data = read_mcp(home)?;
    let servers = data.entry("mcpServers").or_insert_with(|| json!({}));
    let servers = servers.as_object_mut().ok_or("mcp.json is not valid. Fix or remove it first.")?;
    if servers.contains_key(name) {
        return Err(format!("There is already a server called {name}."));
    }

    let clean = |value: &Option<String>| value.as_deref().map(str::trim).filter(|v| !v.is_empty()).map(str::to_owned);
    let spec = match (clean(&server.command), clean(&server.url)) {
        (Some(command), None) => {
            let mut spec = json!({"command": command, "args": server.args});
            if !server.env.is_empty() {
                spec["env"] = Value::Object(server.env);
            }
            spec
        }
        (None, Some(url)) => {
            if !(url.starts_with("https://") || url.starts_with("http://")) {
                return Err("The URL has to start with http:// or https://.".to_owned());
            }
            let mut spec = json!({"url": url});
            match (clean(&server.bearer_token), server.oauth) {
                (Some(_), true) => return Err("Use a bearer token or OAuth, not both.".to_owned()),
                (Some(token), false) => spec["headers"] = json!({"Authorization": format!("Bearer {token}")}),
                (None, true) => spec["auth"] = json!("oauth"),
                (None, false) => {}
            }
            spec
        }
        _ => return Err("A server needs a command or a URL.".to_owned()),
    };
    servers.insert(name.to_owned(), spec);
    write_mcp(home, &data)
}

/// Also forgets its OAuth tokens.
pub fn remove_mcp(home: &Path, name: &str) -> Result<(), String> {
    if !valid_mcp_name(name) {
        return Err("That is not a server's name.".to_owned());
    }
    let mut data = read_mcp(home)?;
    if let Some(Value::Object(servers)) = data.get_mut("mcpServers") {
        servers.remove(name);
    }
    write_mcp(home, &data)?;
    let _ = fs::remove_file(home.join("mcp-auth").join(format!("{name}.json")));
    Ok(())
}

/// This computer hosts the agent's own server and the app's command channel (ipc.localhost), and an
/// app declaring them could reach both. So a page can only connect to named hosts elsewhere: no
/// localhost, and no IP addresses, which also rules out their many alternative spellings.
fn is_named_remote_host(source: &str) -> bool {
    let host = source.split_once("://").map_or(source, |(_, rest)| rest);
    let host = host.split(['/', ':']).next().unwrap_or(host).trim_end_matches('.').to_ascii_lowercase();
    let last = host.rsplit('.').next().unwrap_or("");
    host.contains('.') && last != "localhost" && !last.is_empty() && last.chars().all(|c| c.is_ascii_alphabetic())
}

/// A domain from a server is only used if it could not add a directive of its own.
fn safe_sources(csp: Option<&Value>, key: &str) -> Vec<String> {
    let listed = csp.and_then(|csp| csp.get(key)).and_then(Value::as_array);
    listed
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|source| is_named_remote_host(source))
        .filter(|source| source.chars().all(|c| c.is_ascii_alphanumeric() || ".:/*_-".contains(c)))
        .map(str::to_owned)
        .collect()
}

/// The Content-Security-Policy for an MCP App's page, from what its resource declares. Nothing
/// is reachable unless declared. As in the spec's defaults, with no `csp` nothing can connect out.
pub fn app_csp(csp: Option<&Value>) -> String {
    let with = |base: &str, key: &str| {
        let extra = safe_sources(csp, key).join(" ");
        if extra.is_empty() { base.to_owned() } else { format!("{base} {extra}") }
    };
    let or_none = |key: &str| {
        let sources = safe_sources(csp, key).join(" ");
        if sources.is_empty() { "'none'".to_owned() } else { sources }
    };
    format!(
        "default-src 'none'; script-src {}; style-src {}; img-src {}; font-src {}; media-src {}; connect-src {}; frame-src {}; object-src 'none'; base-uri {}; form-action 'none'",
        with("'self' 'unsafe-inline'", "resourceDomains"),
        with("'self' 'unsafe-inline'", "resourceDomains"),
        with("'self' data:", "resourceDomains"),
        with("'self'", "resourceDomains"),
        with("'self' data:", "resourceDomains"),
        or_none("connectDomains"),
        or_none("frameDomains"),
        with("'self'", "baseUriDomains"),
    )
}

/// Adds a rating to feedback.jsonl, which only this user can read, since it quotes the conversation.
/// Nothing sends it anywhere. Ratings can change, so for a response the last line counts.
pub fn save_feedback(home: &Path, feedback: &Value) -> Result<(), String> {
    let path = home.join("feedback.jsonl");
    let describe = |e: std::io::Error| format!("Could not save the rating: {e}");
    fs::create_dir_all(home).map_err(describe)?;
    let mut options = fs::OpenOptions::new();
    options.append(true).create(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    let mut line = serde_json::to_string(feedback).map_err(|e| e.to_string())?;
    line.push('\n');
    options.open(&path).map_err(describe)?.write_all(line.as_bytes()).map_err(describe)
}

#[derive(Serialize, Debug, PartialEq)]
pub struct Skill {
    pub name: String,
    pub description: String,
    /// Project skills come with the working folder, so they are managed there.
    pub scope: &'static str,
}

/// The rules the agent's skill loader applies, so that a skill it would skip is refused here.
fn valid_skill_name(name: &str) -> bool {
    name.len() <= 64
        && !name.is_empty()
        && !name.starts_with('-')
        && !name.ends_with('-')
        && !name.contains("--")
        && name.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

fn read_skill(folder: &Path) -> Result<(String, String), String> {
    let text = fs::read_to_string(folder.join("SKILL.md")).map_err(|_| "That folder has no SKILL.md.".to_owned())?;
    let front = text
        .strip_prefix("---\n")
        .or_else(|| text.strip_prefix("---\r\n"))
        .and_then(|rest| rest.split_once("\n---"))
        .ok_or("SKILL.md has to start with a --- block holding its name and description.")?
        .0;
    let data: serde_norway::Value = serde_norway::from_str(front).map_err(|e| format!("SKILL.md's --- block is not valid: {e}"))?;
    let text_of = |key: &str| data.get(key).and_then(|value| value.as_str()).map(str::trim).unwrap_or("").to_owned();
    let (name, description) = (text_of("name"), text_of("description"));
    if name.is_empty() || description.is_empty() {
        return Err("SKILL.md needs a name and a description.".to_owned());
    }
    Ok((name, description))
}

fn skills_in(folder: &Path, scope: &'static str, found: &mut Vec<Skill>) {
    let Ok(entries) = fs::read_dir(folder) else { return };
    for entry in entries.flatten() {
        if let Ok((name, description)) = read_skill(&entry.path()) {
            found.push(Skill { name, description, scope });
        }
    }
}

pub fn list_skills(home: &Path, workspace: Option<&Path>) -> Vec<Skill> {
    let mut found = Vec::new();
    skills_in(&home.join("agents/skills"), "personal", &mut found);
    if let Some(workspace) = workspace {
        skills_in(&workspace.join(".agents/skills"), "project", &mut found);
    }
    found.sort_by(|a, b| a.name.cmp(&b.name));
    found
}

fn copy_folder(from: &Path, to: &Path) -> std::io::Result<()> {
    fs::create_dir_all(to)?;
    for entry in fs::read_dir(from)? {
        let entry = entry?;
        // Links are left behind: following one could copy files from outside the skill.
        let kind = entry.file_type()?;
        if kind.is_dir() {
            copy_folder(&entry.path(), &to.join(entry.file_name()))?;
        } else if kind.is_file() {
            fs::copy(entry.path(), to.join(entry.file_name()))?;
        }
    }
    Ok(())
}

/// Copies a skill's folder into the agent's skills, under the name in its SKILL.md.
pub fn add_skill(home: &Path, source: &Path) -> Result<String, String> {
    let (name, _) = read_skill(source)?;
    if !valid_skill_name(&name) {
        return Err(format!("{name} is not a valid skill name: use lowercase letters, numbers and single hyphens."));
    }
    let skills = home.join("agents/skills");
    let target = skills.join(&name);
    if target.exists() {
        return Err(format!("There is already a skill called {name}."));
    }
    // Into a temporary folder first, so a failed copy never looks like a skill.
    let temporary = skills.join(format!(".adding-{name}"));
    let _ = fs::remove_dir_all(&temporary);
    let copied = copy_folder(source, &temporary).and_then(|()| fs::rename(&temporary, &target));
    if let Err(error) = copied {
        let _ = fs::remove_dir_all(&temporary);
        return Err(format!("Could not copy the skill: {error}"));
    }
    Ok(name)
}

pub fn remove_skill(home: &Path, name: &str) -> Result<(), String> {
    if !valid_skill_name(name) {
        return Err("That is not a skill's name.".to_owned());
    }
    fs::remove_dir_all(home.join("agents/skills").join(name)).map_err(|e| format!("Could not remove {name}: {e}"))
}

/// What Settings can change in config.yaml. The defaults match the CLI's. The file also holds the API
/// key, which is why this is a list of fields and not the file: nothing else goes to the webview.
#[derive(Serialize, Deserialize, Debug, PartialEq, Clone)]
#[serde(rename_all = "camelCase")]
pub struct AgentSettings {
    pub commands_enabled: bool,
    pub commands_network: bool,
    pub commands_timeout: u32,
    pub web_enabled: bool,
    pub allowed_hosts: Vec<String>,
    pub obey_robots: bool,
    pub browser: String,
    pub search_url: String,
}

impl Default for AgentSettings {
    fn default() -> Self {
        Self {
            commands_enabled: true,
            commands_network: false,
            commands_timeout: 120,
            web_enabled: true,
            allowed_hosts: Vec::new(),
            obey_robots: true,
            browser: String::new(),
            search_url: String::new(),
        }
    }
}

const MAX_COMMAND_SECONDS: u32 = 24 * 60 * 60;

fn read_config(home: &Path) -> Result<serde_norway::Value, String> {
    let text = fs::read_to_string(home.join("config.yaml")).map_err(|e| format!("Could not read config.yaml: {e}"))?;
    let config: serde_norway::Value = serde_norway::from_str(&text).map_err(|_| "config.yaml is not valid. Fix it first.".to_owned())?;
    if config.is_mapping() { Ok(config) } else { Err("config.yaml is not valid. Fix it first.".to_owned()) }
}

pub fn read_settings(home: &Path) -> Result<AgentSettings, String> {
    let config = read_config(home)?;
    let mut settings = AgentSettings::default();
    let section = |name: &str, key: &str| config.get(name).and_then(|part| part.get(key)).cloned();
    let flag = |name: &str, key: &str, default: bool| section(name, key).and_then(|v| v.as_bool()).unwrap_or(default);
    let text = |name: &str, key: &str| section(name, key).and_then(|v| v.as_str().map(str::to_owned)).unwrap_or_default();
    settings.commands_enabled = flag("sandbox", "enabled", settings.commands_enabled);
    settings.commands_network = flag("sandbox", "network", settings.commands_network);
    if let Some(seconds) = section("sandbox", "timeout_seconds").and_then(|v| v.as_u64()) {
        settings.commands_timeout = u32::try_from(seconds).unwrap_or(MAX_COMMAND_SECONDS);
    }
    settings.web_enabled = flag("web", "enabled", settings.web_enabled);
    settings.obey_robots = flag("web", "obey_robots", settings.obey_robots);
    settings.browser = text("web", "browser");
    settings.search_url = text("web", "search_url");
    if let Some(hosts) = section("web", "allowed_hosts").as_ref().and_then(|v| v.as_sequence()) {
        settings.allowed_hosts = hosts.iter().filter_map(|host| host.as_str().map(str::to_owned)).collect();
    }
    Ok(settings)
}

/// Changes these fields and leaves the rest of the file as it was, apart from any comments in it.
pub fn write_settings(home: &Path, settings: AgentSettings) -> Result<(), String> {
    use serde_norway::{Mapping, Value as Yaml};

    if settings.commands_timeout == 0 || settings.commands_timeout > MAX_COMMAND_SECONDS {
        return Err("The command time limit has to be between 1 second and a day.".to_owned());
    }
    let search_url = settings.search_url.trim();
    if !search_url.is_empty()
        && !(search_url.contains("{query}") && (search_url.starts_with("https://") || search_url.starts_with("http://")))
    {
        return Err("The search page has to be a web address containing {query}.".to_owned());
    }
    let optional = |text: &str| match text.trim() {
        "" => Yaml::Null,
        text => Yaml::String(text.to_owned()),
    };
    let hosts: Vec<Yaml> = settings
        .allowed_hosts
        .iter()
        .map(|host| host.trim())
        .filter(|host| !host.is_empty())
        .map(|host| Yaml::String(host.to_owned()))
        .collect();

    let mut config = read_config(home)?;
    let set = |config: &mut Yaml, name: &str, values: Vec<(&str, Yaml)>| -> Result<(), String> {
        let root = config.as_mapping_mut().ok_or("config.yaml is not valid. Fix it first.")?;
        let part = root.entry(Yaml::String(name.to_owned())).or_insert_with(|| Yaml::Mapping(Mapping::new()));
        if !part.is_mapping() {
            *part = Yaml::Mapping(Mapping::new());
        }
        let part = part.as_mapping_mut().ok_or("config.yaml is not valid. Fix it first.")?;
        for (key, value) in values {
            part.insert(Yaml::String(key.to_owned()), value);
        }
        Ok(())
    };
    set(
        &mut config,
        "sandbox",
        vec![
            ("enabled", Yaml::Bool(settings.commands_enabled)),
            ("network", Yaml::Bool(settings.commands_network)),
            ("timeout_seconds", Yaml::Number(settings.commands_timeout.into())),
        ],
    )?;
    set(
        &mut config,
        "web",
        vec![
            ("enabled", Yaml::Bool(settings.web_enabled)),
            ("allowed_hosts", Yaml::Sequence(hosts)),
            ("obey_robots", Yaml::Bool(settings.obey_robots)),
            ("browser", optional(&settings.browser)),
            ("search_url", optional(search_url)),
        ],
    )?;
    let text = serde_norway::to_string(&config).map_err(|e| e.to_string())?;
    write_private(&home.join("config.yaml"), &text)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Temp(PathBuf);

    impl Temp {
        fn new(label: &str) -> Self {
            let path = std::env::temp_dir().join(format!("steve-addons-{label}-{}", std::process::id()));
            let _ = fs::remove_dir_all(&path);
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for Temp {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn command(name: &str) -> NewMcpServer {
        NewMcpServer { name: name.into(), command: Some("run-it".into()), args: vec!["--token".into(), "s3cret".into()], ..Default::default() }
    }

    fn url(name: &str) -> NewMcpServer {
        NewMcpServer { name: name.into(), url: Some("https://example.test/mcp?key=s3cret".into()), ..Default::default() }
    }

    #[test]
    fn listing_never_returns_secrets() {
        let home = Temp::new("secrets");
        add_mcp(&home.0, command("a")).unwrap();
        add_mcp(&home.0, NewMcpServer { bearer_token: Some("s3cret".into()), ..url("b") }).unwrap();
        add_mcp(&home.0, NewMcpServer { oauth: true, ..url("c") }).unwrap();
        let listed = list_mcp(&home.0).unwrap();
        assert!(!format!("{listed:?}").contains("s3cret"));
        assert_eq!(listed.iter().map(|e| e.auth).collect::<Vec<_>>(), ["none", "bearer", "oauth"]);
        let saved = fs::read_to_string(home.0.join("mcp.json")).unwrap();
        assert!(saved.contains("Bearer s3cret"), "the CLI still needs the token");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(home.0.join("mcp.json")).unwrap().permissions().mode() & 0o077, 0);
        }
    }

    #[test]
    fn bad_servers_are_refused_and_other_settings_survive() {
        let home = Temp::new("refused");
        fs::write(home.0.join("mcp.json"), r#"{"other": 1, "mcpServers": {"kept": {"url": "https://k", "custom": true}}}"#).unwrap();
        add_mcp(&home.0, command("a")).unwrap();
        assert!(add_mcp(&home.0, command("a")).unwrap_err().contains("already"));
        assert!(add_mcp(&home.0, command("bad name")).is_err());
        assert!(add_mcp(&home.0, NewMcpServer { name: "n".into(), ..Default::default() }).is_err());
        assert!(add_mcp(&home.0, NewMcpServer { url: Some("ftp://x".into()), ..url("n") }).is_err());
        assert!(add_mcp(&home.0, NewMcpServer { oauth: true, bearer_token: Some("t".into()), ..url("n") }).is_err());
        let saved: Value = serde_json::from_str(&fs::read_to_string(home.0.join("mcp.json")).unwrap()).unwrap();
        assert_eq!(saved["other"], 1);
        assert_eq!(saved["mcpServers"]["kept"]["custom"], true);
    }

    #[test]
    fn broken_file_is_not_overwritten() {
        let home = Temp::new("broken");
        fs::write(home.0.join("mcp.json"), "{oops").unwrap();
        assert!(add_mcp(&home.0, command("a")).is_err());
        assert_eq!(fs::read_to_string(home.0.join("mcp.json")).unwrap(), "{oops");
    }

    #[test]
    fn removing_a_server_forgets_its_tokens() {
        let home = Temp::new("remove");
        add_mcp(&home.0, NewMcpServer { oauth: true, ..url("a") }).unwrap();
        fs::create_dir_all(home.0.join("mcp-auth")).unwrap();
        fs::write(home.0.join("mcp-auth/a.json"), "{}").unwrap();
        remove_mcp(&home.0, "a").unwrap();
        assert!(list_mcp(&home.0).unwrap().is_empty());
        assert!(!home.0.join("mcp-auth/a.json").exists());
        assert!(remove_mcp(&home.0, "../a").is_err());
    }

    #[test]
    fn feedback_is_appended_and_private() {
        let home = Temp::new("feedback");
        save_feedback(&home.0, &json!({"rating": "good"})).unwrap();
        save_feedback(&home.0, &json!({"rating": null})).unwrap();
        let saved = fs::read_to_string(home.0.join("feedback.jsonl")).unwrap();
        assert_eq!(saved, "{\"rating\":\"good\"}\n{\"rating\":null}\n");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(home.0.join("feedback.jsonl")).unwrap().permissions().mode() & 0o077, 0);
        }
    }

    #[test]
    fn an_app_can_reach_only_what_it_declares() {
        let none = app_csp(None);
        assert!(none.contains("connect-src 'none'"));
        assert!(none.contains("default-src 'none'"));
        let declared = json!({"connectDomains": ["https://api.example.com"], "resourceDomains": ["https://cdn.example.com"]});
        let csp = app_csp(Some(&declared));
        assert!(csp.contains("connect-src https://api.example.com;"));
        assert!(csp.contains("script-src 'self' 'unsafe-inline' https://cdn.example.com;"));
        assert!(csp.contains("frame-src 'none'"));
    }

    #[test]
    fn a_declared_domain_cannot_widen_the_policy() {
        let hostile = json!({"connectDomains": ["*", "https:", "https://a.example; script-src *", "'unsafe-eval'", "https://ok.example"]});
        let csp = app_csp(Some(&hostile));
        assert!(csp.contains("connect-src https://ok.example;"), "{csp}");
        assert!(!csp.contains("unsafe-eval"));
    }

    #[test]
    fn a_declared_domain_cannot_reach_this_computer() {
        let hostile = json!({"connectDomains": [
            "http://127.0.0.1:*", "http://127.0.0.1:8000", "http://localhost:3000", "http://ipc.localhost",
            "http://tauri.localhost", "http://mcp-app.localhost", "ipc://localhost", "http://0.0.0.0:80", "http://[::1]:80",
            "HTTP://LOCALHOST.:1", "http://0x7f.0.0.1", "http://2130706433", "http://*.localhost", "https://ok.example"
        ]});
        let csp = app_csp(Some(&hostile));
        assert!(csp.contains("connect-src https://ok.example;"), "{csp}");
    }

    fn skill_folder(root: &Path, folder: &str, name: &str) -> PathBuf {
        let path = root.join(folder);
        fs::create_dir_all(path.join("scripts")).unwrap();
        fs::write(path.join("SKILL.md"), format!("---\nname: {name}\ndescription: Does things.\n---\n\nBody\n")).unwrap();
        fs::write(path.join("scripts/run.py"), "print(1)").unwrap();
        path
    }

    #[test]
    fn skills_are_copied_under_their_own_name_and_can_be_removed() {
        let root = Temp::new("skills");
        let home = root.0.join("home");
        let source = skill_folder(&root.0, "downloaded-copy", "my-skill");
        assert_eq!(add_skill(&home, &source).unwrap(), "my-skill");
        assert!(home.join("agents/skills/my-skill/scripts/run.py").is_file());
        assert!(source.join("SKILL.md").is_file(), "the original stays");
        assert!(add_skill(&home, &source).unwrap_err().contains("already"));

        let workspace = root.0.join("project");
        skill_folder(&workspace.join(".agents/skills"), "local-one", "local-one");
        let listed = list_skills(&home, Some(&workspace));
        assert_eq!(listed.len(), 2);
        assert_eq!(listed[0].scope, "project");
        assert_eq!(listed[1], Skill { name: "my-skill".into(), description: "Does things.".into(), scope: "personal" });

        remove_skill(&home, "my-skill").unwrap();
        assert!(!home.join("agents/skills/my-skill").exists());
        assert!(remove_skill(&home, "../../x").is_err());
    }

    #[test]
    fn folders_the_agent_could_not_load_are_refused() {
        let root = Temp::new("bad-skills");
        let home = root.0.join("home");
        let empty = root.0.join("empty");
        fs::create_dir_all(&empty).unwrap();
        assert!(add_skill(&home, &empty).unwrap_err().contains("SKILL.md"));
        assert!(add_skill(&home, &skill_folder(&root.0, "x", "Bad_Name")).unwrap_err().contains("not a valid"));
        let plain = root.0.join("plain");
        fs::create_dir_all(&plain).unwrap();
        fs::write(plain.join("SKILL.md"), "no front matter").unwrap();
        assert!(add_skill(&home, &plain).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn links_in_a_skill_are_not_followed() {
        let root = Temp::new("links");
        let home = root.0.join("home");
        let source = skill_folder(&root.0, "linked", "linked");
        fs::write(root.0.join("secret"), "s3cret").unwrap();
        std::os::unix::fs::symlink(root.0.join("secret"), source.join("stolen")).unwrap();
        add_skill(&home, &source).unwrap();
        assert!(!home.join("agents/skills/linked/stolen").exists());
    }

    fn config_home(label: &str, text: &str) -> (Temp, PathBuf) {
        let dir = Temp::new(label);
        let home = dir.0.clone();
        fs::write(home.join("config.yaml"), text).unwrap();
        (dir, home)
    }

    #[test]
    fn settings_default_to_the_safe_choices() {
        let (_dir, home) = config_home("settings-defaults", "model:\n  api_key: sk-secret\n");
        let settings = read_settings(&home).unwrap();
        assert_eq!(settings, AgentSettings::default());
        assert!(!settings.commands_network && settings.obey_robots && settings.allowed_hosts.is_empty());
    }

    #[test]
    fn saving_settings_keeps_the_rest_of_the_file_and_stays_private() {
        let (_dir, home) = config_home("settings-keep", "model:\n  api_key: sk-secret\nweb:\n  timeout_seconds: 9\nserver:\n  port: 1234\n");
        let wanted = AgentSettings {
            commands_network: true,
            commands_timeout: 30,
            allowed_hosts: vec!["10.0.0.0/8".into(), "  ".into(), " nas.local ".into()],
            obey_robots: false,
            browser: "chrome".into(),
            search_url: "https://example.com/?q={query}".into(),
            ..AgentSettings::default()
        };
        write_settings(&home, wanted.clone()).unwrap();
        let text = fs::read_to_string(home.join("config.yaml")).unwrap();
        assert!(text.contains("sk-secret") && text.contains("port: 1234") && text.contains("timeout_seconds: 9"));
        let saved = read_settings(&home).unwrap();
        assert_eq!(saved.allowed_hosts, vec!["10.0.0.0/8".to_owned(), "nas.local".to_owned()]);
        assert_eq!(AgentSettings { allowed_hosts: wanted.allowed_hosts.clone(), ..saved }, wanted);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(home.join("config.yaml")).unwrap().permissions().mode() & 0o777, 0o600);
        }
    }

    #[test]
    fn empty_choices_are_saved_as_nothing() {
        let (_dir, home) = config_home("settings-empty", "model: {}\n");
        write_settings(&home, AgentSettings::default()).unwrap();
        let text = fs::read_to_string(home.join("config.yaml")).unwrap();
        assert!(text.contains("browser: null") && text.contains("search_url: null"));
        assert_eq!(read_settings(&home).unwrap(), AgentSettings::default());
    }

    #[test]
    fn bad_settings_are_refused_and_change_nothing() {
        let original = "model: {}\n";
        let (_dir, home) = config_home("settings-bad", original);
        for bad in [
            AgentSettings { commands_timeout: 0, ..AgentSettings::default() },
            AgentSettings { commands_timeout: MAX_COMMAND_SECONDS + 1, ..AgentSettings::default() },
            AgentSettings { search_url: "https://example.com/".into(), ..AgentSettings::default() },
            AgentSettings { search_url: "file:///{query}".into(), ..AgentSettings::default() },
        ] {
            assert!(write_settings(&home, bad).is_err());
        }
        assert_eq!(fs::read_to_string(home.join("config.yaml")).unwrap(), original);
    }

    #[test]
    fn a_broken_config_is_not_overwritten() {
        let (_dir, home) = config_home("settings-broken", ": not yaml: [");
        assert!(read_settings(&home).is_err());
        assert!(write_settings(&home, AgentSettings::default()).is_err());
        assert_eq!(fs::read_to_string(home.join("config.yaml")).unwrap(), ": not yaml: [");
    }
}
