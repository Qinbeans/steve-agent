// Drives the real desktop app over WebDriver: UI -> Rust -> real steve-agent CLI -> real agent -> fake model.
// Runs with a throwaway HOME, so it never sees your config or your model.
//
// Needs: `pnpm tauri build --debug --no-bundle`, `uv sync` in steve-cli, `cargo install tauri-driver`,
// and WebKitWebDriver. Set SHOTS to a directory to keep screenshots.

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const cli = resolve(root, "../steve-cli");
const app = join(root, "src-tauri/target/debug/steve-desktop");
const driverUrl = "http://127.0.0.1:4444";
const shots = process.env.SHOTS;
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const processes = [];

function firstLine(child) {
	return new Promise((done) => createInterface({ input: child.stdout }).once("line", done));
}

async function wd(method, path, body) {
	const response = await fetch(driverUrl + path, {
		method,
		headers: { "content-type": "application/json" },
		body: body === undefined ? undefined : JSON.stringify(body),
	});
	const { value } = await response.json();
	if (!response.ok) throw new Error(`${method} ${path}: ${JSON.stringify(value).slice(0, 300)}`);
	return value;
}

const ELEMENT = "element-6066-11e4-a52e-4f735466cecf";
let session;
const css = (selector) => `[data-testid="${selector}"]`;

async function all(selector) {
	return (await wd("POST", `/session/${session}/elements`, { using: "css selector", value: selector })).map((e) => e[ELEMENT]);
}
const textOf = (id) => wd("GET", `/session/${session}/element/${id}/text`);
// Retries, because a menu's items cannot be clicked until its opening animation ends.
const click = (element) =>
	until("a click to land", async () => {
		await wd("POST", `/session/${session}/element/${element}/click`, {});
		return true;
	});

async function until(what, check, timeout = 30000) {
	const deadline = Date.now() + timeout;
	let last;
	while (Date.now() < deadline) {
		try {
			last = await check();
			if (last) return last;
		} catch (error) {
			last = error.message;
		}
		await sleep(150);
	}
	throw new Error(`Timed out waiting for ${what}. Last: ${last}`);
}

const one = (selector) => until(selector, async () => (await all(selector))[0]);
const texts = async (selector) => Promise.all((await all(selector)).map(textOf));

async function screenshot(name) {
	if (!shots) return;
	mkdirSync(shots, { recursive: true });
	const png = await wd("GET", `/session/${session}/screenshot`);
	writeFileSync(join(shots, `${name}.png`), Buffer.from(png, "base64"));
}

async function say(text) {
	const box = await one(css("composer"));
	await until("the composer to be enabled", async () => wd("GET", `/session/${session}/element/${box}/enabled`));
	await wd("POST", `/session/${session}/element/${box}/value`, { text });
	await click(await one(css("send")));
}

// Reads textContent: WebDriver's getText reports what is visible, which is unreliable during the sheet's animation.
const domText = (selector) =>
	wd("POST", `/session/${session}/execute/sync`, {
		script: "return document.querySelector(arguments[0])?.textContent.trim()",
		args: [selector],
	});

const lastAnswer = async () => (await texts(css("assistant-message"))).at(-1) ?? "";

const home = mkdtempSync(join(tmpdir(), "steve-e2e-"));
let failed = false;
try {
	const upstream = spawn(join(cli, ".venv/bin/python"), [join(root, "e2e/upstream.py")], { stdio: ["pipe", "pipe", "inherit"] });
	processes.push(upstream);
	const baseUrl = await firstLine(upstream);

	mkdirSync(join(home, ".config/steve/agents/memories"), { recursive: true });
	mkdirSync(join(home, ".config/steve/agents/skills"), { recursive: true });
	writeFileSync(join(home, ".config/steve/agents/AGENTS.md"), "");
	writeFileSync(join(home, ".config/steve/agents/memories/MEMORY.md"), "");
	writeFileSync(join(home, ".config/steve/config.yaml"), `model:\n  model: fake\n  base_url: ${baseUrl}\n  api_key: k\n`);

	// The folder picker is a native dialog that WebDriver cannot reach, so folders come from the saved list.
	const project = join(home, "my-project");
	const second = join(home, "second-project");
	mkdirSync(project);
	mkdirSync(second);
	const seededSkill = join(home, ".config/steve/agents/skills/e2e-skill");
	mkdirSync(seededSkill);
	writeFileSync(join(seededSkill, "SKILL.md"), "---\nname: e2e-skill\ndescription: A skill for the test.\n---\n\nDo the thing.\n");
	const settings = join(home, ".config/com.steve.steve-desktop");
	mkdirSync(settings, { recursive: true });
	writeFileSync(join(settings, "settings.json"), JSON.stringify({ workspace: null, recent: [project, second, join(home, "gone")] }));

	// Only the first start is slowed, so the splash can be seen for certain.
	const slowCli = join(home, "slow-steve-agent");
	const realCli = join(cli, ".venv/bin/steve-agent");
	const slowMark = join(home, "slowed-once");
	writeFileSync(slowCli, `#!/bin/sh\nif [ ! -e "${slowMark}" ]; then touch "${slowMark}"; sleep 6; fi\nexec "${realCli}" "$@"\n`, { mode: 0o755 });

	const driver = spawn(process.env.TAURI_DRIVER ?? "tauri-driver", ["--port", "4444"], {
		env: { ...process.env, HOME: home, XDG_CONFIG_HOME: join(home, ".config"), STEVE_AGENT_BIN: slowCli },
		stdio: "inherit",
	});
	processes.push(driver);
	await until("tauri-driver", async () => (await fetch(`${driverUrl}/status`)).ok);

	session = (
		await wd("POST", "/session", {
			capabilities: { alwaysMatch: { browserName: "wry", "tauri:options": { application: app } } },
		})
	).sessionId;

	console.log("• a splash shows until the agent is ready");
	const splashText = () => domText(css("splash"));
	await until("the splash to name the agent", async () => (await splashText())?.includes("Starting the agent"));
	const covered = await wd("POST", `/session/${session}/execute/sync`, {
		script: "const box = document.getElementById('splash').getBoundingClientRect(); return [box.width === innerWidth, box.height === innerHeight, getComputedStyle(document.getElementById('splash')).position, getComputedStyle(document.getElementById('splash')).backgroundColor]",
		args: [],
	});
	if (!covered[0] || !covered[1] || covered[2] !== "fixed") throw new Error(`The splash should cover the window: ${covered}`);
	await screenshot("0-splash");
	console.log(`  splash says: ${await splashText()}`);
	await until("the splash to go", async () => (await all(css("splash"))).length === 0, 60000);

	console.log("• the mode starts on manual, and can be changed");
	await until("the mode selector", async () => (await domText(css("mode-name"))) === "Manual");
	const setMode = async (id) => {
		await click(await one(css("mode-button")));
		await click(await one(css(`mode-${id}`)));
		await until(`mode ${id}`, async () => (await domText(css("mode-name"))).toLowerCase() === id);
	};
	await setMode("auto"); // The checks below are about other things, and manual would pause them.

	console.log("• the app starts with no dialog and no folder");
	await until("the agent to be ready", async () => (await all(css("empty-chat"))).length > 0, 60000);
	if ((await domText(css("workspace-name"))) !== "No folder") throw new Error("The selector should say No folder");
	if ((await all(css("workspace-confirm-dialog"))).length) throw new Error("There should be no starting dialog");
	await screenshot("0-no-folder");

	console.log("• the dropdown lists recent folders, leaving out missing ones");
	await click(await one(css("workspace-button")));
	await one(css("workspace-other"));
	await screenshot("0-folder-menu");
	const seeded = await texts(css("workspace-recent"));
	if (seeded.length !== 2) throw new Error(`Expected 2 recent folders, got ${seeded}`);
	await click((await all(css("workspace-recent")))[0]);

	console.log("• the app starts the CLI in that folder");
	await until("the selector to name the folder", async () => (await domText(css("workspace-name"))) === "my-project", 60000);
	await until("the agent to be ready", async () => (await all(css("empty-chat"))).length > 0, 60000);
	await screenshot("1-ready");

	console.log("• tools, a subagent and Markdown");
	await say("Please delegate this");
	await until("the first answer", async () => (await lastAnswer()).includes("Parent done."));
	const tools = await all(css("tool-call"));
	if (tools.length !== 2) throw new Error(`Expected 2 tool rows, got ${tools.length}`);
	const heading = await all('[data-testid="assistant-message"] h2');
	if (heading.length !== 1) throw new Error("Markdown did not render");
	await screenshot("2-answer");
	if (readFileSync(join(project, "from-agent.txt"), "utf8") !== "written by the agent")
		throw new Error("The agent's file did not land in the chosen folder");

	console.log("• reasoning folds away and opens on click");
	if ((await all(css("reasoning"))).length !== 3) throw new Error("Expected reasoning on each of the 3 model calls");
	const visible = async (id) => wd("GET", `/session/${session}/element/${id}/displayed`);
	for (const text of await all(css("reasoning-text"))) {
		if (await visible(text)) throw new Error("Finished reasoning should be folded");
	}
	await click(await one(`${css("reasoning")} button`));
	await until("the reasoning text", async () => {
		const [first] = await all(css("reasoning-text"));
		return (await visible(first)) && (await textOf(first)).includes("Thinking part A.");
	});
	await screenshot("2b-reasoning");

	console.log("• agent output cannot run scripts");
	if (await wd("POST", `/session/${session}/execute/sync`, { script: "return document.title", args: [] }) === "pwned")
		throw new Error("HTML in agent output ran");
	if ((await all('[data-testid="assistant-message"] img')).length) throw new Error("An image from agent output was rendered");

	console.log("• watching the subagent");
	await click(await one(css("watch-subagent")));
	await one(css("subagent-sheet"));
	await until("the subagent's text", async () => (await textOf(await one(`${css("subagent-sheet")} ${css("assistant-message")}`))).includes("PONG"));
	await until("the subagent to finish", async () => (await domText(css("subagent-status"))) === "completed");
	await screenshot("3-subagent");
	await wd("POST", `/session/${session}/actions`, {
		actions: [{ type: "key", id: "k", actions: [{ type: "keyDown", value: "" }, { type: "keyUp", value: "" }] }],
	});
	await until("the sheet to close", async () => (await all(css("subagent-sheet"))).length === 0);

	console.log("• continuing the conversation");
	await say("Do you remember?");
	await until("the second answer", async () => (await lastAnswer()).includes("I remember that."));

	console.log("• a failed model call recovers through the retry");
	await say("Try something that fails");
	await until("the retried answer", async () => (await lastAnswer()).includes("Recovered after a retry."));
	if ((await all(css("turn-error"))).length) throw new Error("A recovered failure was shown as an error");

	console.log("• stopping a run");
	await say("Go on forever");
	await until("streaming to start", async () => (await lastAnswer()).includes("This"));
	await click(await one(css("stop")));
	await one(css("turn-cancelled"));

	console.log("• the context pie shows the breakdown and compacts on click");
	const pie = await one(css("context-pie"));
	const usedBefore = Number(await wd("GET", `/session/${session}/element/${pie}/attribute/data-used`));
	if (!usedBefore) throw new Error("The pie should report tokens");
	await wd("POST", `/session/${session}/actions`, {
		actions: [
			{ type: "pointer", id: "mouse", actions: [{ type: "pointerMove", duration: 0, origin: { [ELEMENT]: pie }, x: 0, y: 0 }] },
		],
	});
	await until("the breakdown", async () => (await domText(css("context-breakdown")))?.includes("Tool results"));
	await screenshot("4-context");
	await click(pie);
	await until("the compaction to finish", async () => {
		const turns = await texts(css("assistant-message"));
		return turns.length > 0 && /summarized|compact/i.test(turns.at(-1));
	});

	console.log("• a file sent with a message reaches the agent");
	const asset = join(home, "field-notes.txt");
	writeFileSync(asset, "some field notes");
	// Tauri's own drag and drop event. The native file picker cannot be driven from here.
	await wd("POST", `/session/${session}/execute/sync`, {
		script: "window.__TAURI_INTERNALS__.invoke('plugin:event|emit', { event: 'tauri://drag-drop', payload: arguments[0] })",
		args: [{ paths: [asset], position: { x: 10, y: 10 } }],
	});
	await until("the attachment chip", async () => (await domText(css("pending-attachments")))?.includes("field-notes.txt"));
	await screenshot("5-attachment");
	await say("What is in this file?");
	await until("the attachment to be sent", async () => (await domText(css("sent-attachments")))?.includes("field-notes.txt"));
	await until("the agent's reply", async () => /\/artifacts\/uploads\/.*field-notes\.txt/.test(await lastAnswer()));

	console.log("• a new conversation appears in the list");
	await click(await one(css("new-chat")));
	await until("the empty state", async () => (await all(css("empty-chat"))).length > 0);
	const conversations = await all(css("conversation"));
	if (conversations.length !== 1) throw new Error(`Expected 1 conversation, got ${conversations.length}`);

	console.log("• changing the folder asks first, then restarts the agent there");
	await click(await one(css("workspace-button")));
	await one(css("workspace-other"));
	await click((await all(css("workspace-recent")))[1]);
	if ((await all(css("workspace-confirm-dialog"))).length) throw new Error("With no reply running, there is nothing to warn about");
	await until("the new folder", async () => (await domText(css("workspace-name"))) === "second-project", 60000);
	await until("the agent to be ready", async () => (await all(css("empty-chat"))).length > 0, 60000);
	if ((await all(css("conversation"))).length) throw new Error("Old conversations should be gone");
	await say("Still here?");
	await until("a reply from the restarted agent", async () => (await lastAnswer()).includes("Still here?"));
	if (existsSync(join(second, "from-agent.txt"))) throw new Error("Unexpected file in the new folder");

	console.log("• the chosen folder moves to the top of the list");
	await click(await one(css("workspace-button")));
	await one(css("workspace-other"));
	await until("second-project first, then my-project", async () => {
		const recents = await wd("POST", `/session/${session}/execute/sync`, {
			script: `return [...document.querySelectorAll('[data-testid="workspace-recent"]')].map((item) => item.textContent);`,
			args: [],
		});
		return recents.length === 2 && recents[0].includes("second-project") && recents[1].includes("my-project");
	});

	await click((await all(css("workspace-recent")))[0]); // the current folder, which only closes the menu

	const fill = async (selector, text) => {
		const box = await one(css(selector));
		await wd("POST", `/session/${session}/element/${box}/value`, { text });
	};
	const domTextOf = (element) => wd("POST", `/session/${session}/execute/sync`, { script: "return arguments[0].textContent.trim()", args: [{ "element-6066-11e4-a52e-4f735466cecf": element }] });
	const settleMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
	const dom = (script, ...args) => wd("POST", `/session/${session}/execute/sync`, { script, args });

	console.log("• the skills menu lists and removes a skill");
	await click(await one(css("skills-button")));
	await one(css("skill-add"));
	await until("the seeded skill", async () => (await dom(`return document.querySelector('[data-testid="skill"]')?.textContent`))?.includes("e2e-skill"));
	await screenshot("10-skills");
	await click(await one(css("skill")));
	await click(await one(css("skill-remove")));
	await until("the skill to go", async () => !existsSync(seededSkill));

	console.log("• a server added from the MCP menu connects");
	await click(await one(css("mcp-button")));
	await click(await one(css("mcp-add")));
	await one(css("mcp-add-dialog"));
	await fill("mcp-name", "board");
	await click(await one(css("mcp-kind-command")));
	await fill("mcp-command", join(cli, ".venv/bin/python"));
	await fill("mcp-args", join(root, "e2e/mcp_app_server.py"));
	await screenshot("11-mcp-add");
	await click(await one(css("mcp-save")));
	await until("the dialog to close", async () => (await all(css("mcp-add-dialog"))).length === 0, 60000);
	await until("the composer to be ready", async () => wd("GET", `/session/${session}/element/${await one(css("composer"))}/enabled`), 60000);
	if (!(await all(css("user-message"))).length) throw new Error("A restart should keep the open conversation");
	await click(await one(css("new-chat")));
	await click(await one(css("mcp-button")));
	await until("the server's tools", async () => (await dom(`return document.querySelector('[data-testid="mcp-server"]')?.textContent`))?.includes("5 tools"));
	await screenshot("12-mcp-menu");
	await click(await one(css("mcp-server")));
	await until("the status", async () => (await domText(css("mcp-status"))) === "Connected");
	await dom(`document.querySelector('[data-testid="mcp-details"] [data-slot="dialog-close"]').click()`);
	await until("the details to close", async () => (await all(css("mcp-details"))).length === 0);

	console.log("• an MCP App renders, talks to the host, and cannot get out of its sandbox");
	await dom(`window.__appLog = []; addEventListener("message", (event) => window.__appLog.push(JSON.stringify(event.data).slice(0, 120)));`);
	await say("OPEN BOARD");
	await one(css("mcp-app-frame"));
	const report = await until("the app's message in the composer", async () => {
		const value = await dom(`return document.querySelector('[data-testid="composer"]').value`);
		return value ? JSON.parse(value) : null;
	}, 60000).catch(async (error) => {
		console.error("The app said:", JSON.stringify(await dom("return window.__appLog")));
		throw error;
	});
	await screenshot("13-mcp-app");
	await wd("POST", `/session/${session}/element/${await one(css("composer"))}/clear`, {});
	const loads = (await dom("return window.__appLog")).filter((message) => message.includes("ui/initialize")).length;
	if (loads !== 1) throw new Error(`The app's page should load once, but loaded ${loads} times`);
	console.log("  app report:", JSON.stringify(report));
	const expected = { title: "PLAN", input: "plan", refreshed: "REFRESHED", theme: "dark-or-light", ipc: "blocked", ipcFetch: "blocked", parent: "blocked", storage: "blocked", outside: "blocked", modelOnly: "blocked", protocol: "2026-01-26" };
	for (const [key, want] of Object.entries(expected)) {
		if (report[key] !== want) throw new Error(`The app reported ${key}=${report[key]}, expected ${want}`);
	}
	if ((await dom("return document.title")) === "pwned") throw new Error("The app reached the window");

	console.log("• manual asks first, and a denial changes nothing");
	const folderName = await until("the current folder", () => domText(css("workspace-name")));
	const here = join(home, folderName);
	await click(await one(css("new-chat")));
	await setMode("manual");
	await say("WRITE ALLOWED");
	await until("the approval card", async () => (await domText(css("approval-tool"))) === "write_file");
	if (existsSync(join(here, "allowed.txt"))) throw new Error("Nothing should change before approval");
	await settleMs(300);
	await screenshot("22-approval");
	await click(await one(css("approval-allow")));
	await until("the approved write", async () => existsSync(join(here, "allowed.txt")));
	await until("the card to close", async () => (await all(css("approval-card"))).length === 0);
	await until("the reply", async () => (await lastAnswer()).includes("Wrote it."));

	await say("WRITE DENIED");
	await one(css("approval-card"));
	await fill("approval-reason", "not that file");
	await click(await one(css("approval-deny")));
	await until("the reply to a denial", async () => (await lastAnswer()).includes("I will not."));
	if (existsSync(join(here, "denied.txt"))) throw new Error("A denied write should not happen");

	console.log("• planning changes nothing, and does not ask");
	await setMode("planning");
	await say("PLAN IT");
	await until("the plan", async () => (await lastAnswer()).includes("Step 1"));
	if (existsSync(join(here, "planned.txt"))) throw new Error("Planning should not write");
	if ((await all(css("approval-card"))).length) throw new Error("Planning should not ask");
	await settleMs(300);
	await screenshot("23-planning");

	console.log("• MCP elicitation: the prompt, then choices and/or custom input");
	await setMode("auto");
	const savedOutput = async () => {
		const result = await wd("POST", `/session/${session}/execute/async`, {
			script: `const done = arguments[arguments.length - 1]; const I = window.__TAURI_INTERNALS__; I.invoke("list_conversations").then((list) => I.invoke("get_conversation", { id: list.data[0].id })).then((c) => done(JSON.stringify(c.turns.at(-1).response.output)), (e) => done(String(e)));`,
			args: [],
		});
		return result;
	};
	await say("ASK COLOUR");
	await until("the prompt", async () => (await dom(`return document.querySelector('[data-testid="elicitation-message"] strong')?.textContent`)) === "colour");
	await settleMs(300);
	await screenshot("24-elicit-choices");
	const choices = await all(css("elicitation-choice"));
	if (choices.length !== 2) throw new Error(`Expected 2 choices, got ${choices.length}`);
	await click(await one(css("elicitation-submit")));
	await until("a required choice to be asked for", async () => (await domText(css("elicitation-error"))) === "Choose one.");
	await click(choices[1]);
	await click(await one(css("elicitation-submit")));
	await until("the reply", async () => (await lastAnswer()).includes("Picked."));
	await until("the tool to get green", async () => (await savedOutput()).includes("accept:green"));

	await say("ASK TICKET");
	await one(css("elicitation-form"));
	await click(await one(css("elicitation-submit")));
	await until("the required title", async () => (await domText(css("elicitation-error"))) === "This is required.");
	const input = (name) => one(`[data-field="${name}"] ${css("elicitation-input")}`);
	await wd("POST", `/session/${session}/element/${await input("title")}/value`, { text: "Broken door" });
	await wd("POST", `/session/${session}/element/${await input("count")}/clear`, {});
	await wd("POST", `/session/${session}/element/${await input("count")}/value`, { text: "3" });
	await click(await one(css("elicitation-check")));
	await settleMs(300);
	await screenshot("25-elicit-inputs");
	await click(await one(css("elicitation-submit")));
	await until("the reply", async () => (await lastAnswer()).includes("Filed."));
	await until("the ticket's details", async () => {
		const output = await savedOutput();
		return output.includes("Broken door") && output.includes("urgent=True") && output.includes("count=3");
	});

	await say("ASK NAME");
	await one(css("elicitation-custom"));
	await fill("elicitation-custom", "Radia");
	await settleMs(300);
	await screenshot("26-elicit-custom");
	await click(await one(css("elicitation-submit")));
	await until("the reply", async () => (await lastAnswer()).includes("Named."));
	await until("the typed name", async () => (await savedOutput()).includes("Radia"));

	console.log("• removing the server stops offering it");
	await click(await one(css("mcp-button")));
	await click(await one(css("mcp-server")));
	await click(await one(css("mcp-remove")));
	await until("the details to close", async () => (await all(css("mcp-details"))).length === 0, 60000);
	await until("the composer to be ready", async () => wd("GET", `/session/${session}/element/${await one(css("composer"))}/enabled`), 60000);
	if (readFileSync(join(home, ".config/steve/mcp.json"), "utf8").includes("board")) throw new Error("The server should be gone");

	console.log("• the menu bar is there, and its items reach the UI");
	const inner = await dom("return window.innerHeight");
	const outer = (await wd("GET", `/session/${session}/window/rect`)).height;
	console.log(`  window ${outer}px, webview ${inner}px`);
	if (outer - inner < 20) throw new Error("The webview should sit below a menu bar");
	const fromMenu = (id) => dom(`return window.__TAURI_INTERNALS__.invoke("plugin:event|emit", { event: "menu", payload: arguments[0] })`, id);
	const sidebarState = () => dom(`return document.querySelector('[data-slot="sidebar"][data-state]')?.dataset.state`);
	await fromMenu("toggle-sidebar");
	await until("the sidebar to collapse", async () => (await sidebarState()) === "collapsed");
	if ((await all(css("sidebar-handle"))).length) throw new Error("A collapsed sidebar has nothing to resize");
	await fromMenu("toggle-sidebar");
	await until("the sidebar to open", async () => (await sidebarState()) === "expanded");
	await fromMenu("add-mcp");
	await one(css("mcp-add-dialog"));
	await dom(`document.querySelector('[data-testid="mcp-add-dialog"] [data-slot="dialog-close"]').click()`);
	await until("the dialog to close", async () => (await all(css("mcp-add-dialog"))).length === 0);

	console.log("• the sidebar resizes by dragging and by keyboard, within limits");
	const sidebarWidth = () => dom(`return document.querySelector('[data-slot="sidebar-container"]').getBoundingClientRect().width`);
	const before = await sidebarWidth();
	const dragTo = async (x) => {
		const bar = await one(css("sidebar-handle"));
		const pointer = (actions) => wd("POST", `/session/${session}/actions`, { actions: [{ type: "pointer", id: "mouse", parameters: { pointerType: "mouse" }, actions }] });
		await pointer([
			{ type: "pointerMove", origin: { "element-6066-11e4-a52e-4f735466cecf": bar }, x: 0, y: 0, duration: 0 },
			{ type: "pointerDown", button: 0 },
			{ type: "pointerMove", origin: "viewport", x, y: 300, duration: 100 },
			{ type: "pointerUp", button: 0 },
		]);
	};
	await dragTo(360);
	await until("the sidebar to widen", async () => Math.abs((await sidebarWidth()) - 360) < 4);
	await screenshot("14-wide-sidebar");
	const overflow = await dom(`return [document.documentElement.scrollWidth, window.innerWidth, document.querySelector('[data-slot="sidebar-inset"]').getBoundingClientRect().right]`);
	if (overflow[0] > overflow[1] || overflow[2] > overflow[1]) throw new Error(`The main area should fit beside a wide sidebar: ${overflow}`);
	await dragTo(480);
	await until("the sidebar at its maximum", async () => (await sidebarWidth()) === 480);
	const widest = await dom(`return [document.documentElement.scrollWidth, window.innerWidth, document.querySelector('[data-slot="sidebar-inset"]').getBoundingClientRect().right]`);
	if (widest[0] > widest[1] || widest[2] > widest[1]) throw new Error(`The main area should fit at the maximum: ${widest}`);
	await screenshot("15-widest-sidebar");
	await dragTo(5);
	await until("the sidebar to stop at its minimum", async () => (await sidebarWidth()) === 180);
	const bar = await one(css("sidebar-handle"));
	await wd("POST", `/session/${session}/execute/sync`, { script: "arguments[0].focus()", args: [{ "element-6066-11e4-a52e-4f735466cecf": bar }] });
	await wd("POST", `/session/${session}/element/${bar}/value`, { text: "\uE014" });
	await until("the arrow key to widen it", async () => (await sidebarWidth()) === 196);
	if ((await dom(`return localStorage.getItem("steve.sidebarWidth")`)) !== "196") throw new Error("The width should be remembered");
	console.log(`  width went ${before} -> 360 -> 180 -> 196`);

	console.log("• both themes render");
	const settle = () => new Promise((resolve) => setTimeout(resolve, 600));
	await screenshot("16-empty-a");
	await say("Hello on the plain screen, this is a message long enough to wrap across the conversation. ".repeat(4));
	await until("a reply", async () => (await lastAnswer()).includes("Hello on the plain screen"));
	await dom(`document.querySelector('[data-testid="composer"]').focus()`);
	// Separate calls, because keys held in one call stay held for the rest of it.
	await fill("composer", "**bold words** and \`code\`");
	await fill("composer", "\uE008\uE007");
	await fill("composer", "second line <img src=x onerror=\"document.title='pwned'\"> [link](https://example.com)");
	await click(await one(css("send")));
	await until("the user's message as Markdown", async () => dom(`return [...document.querySelectorAll('[data-testid="user-message"]')].at(-1)?.querySelector("strong")?.textContent === "bold words"`));
	const rendered = await dom(`const box = [...document.querySelectorAll('[data-testid="user-message"]')].at(-1); return [box.querySelector("code")?.textContent, !!box.querySelector("br"), !!box.querySelector("img"), box.querySelector("a")?.getAttribute("href"), document.title]`);
	if (rendered[0] !== "code" || !rendered[1] || rendered[2] || rendered[3] !== "https://example.com" || rendered[4] === "pwned") {
		throw new Error(`A user's Markdown rendered wrongly: ${JSON.stringify(rendered)} from ${await dom(`return [...document.querySelectorAll('[data-testid="user-message"]')].at(-1).innerHTML`)}`);
	}
	await until("the reply", async () => (await lastAnswer()).includes("second line"));
	const tick = await dom(`return getComputedStyle([...document.querySelectorAll('[data-testid="user-message"] code')].at(-1), "::before").content`);
	if (tick.includes("\`")) throw new Error("Inline code should not show literal backticks");
	await settle();
	await screenshot("17-conversation-a");
	const bubble = await dom(`const user = [...document.querySelectorAll('[data-testid="user-message"]')].at(-1).getBoundingClientRect(); const main = document.querySelector('[data-slot="sidebar-inset"]').getBoundingClientRect(); return [user.left, main.left, user.right, main.right]`);
	if (bubble[0] < bubble[1] || bubble[2] > bubble[3]) throw new Error(`A long message should stay in the main area: ${bubble}`);
	await fromMenu("toggle-theme");
	await settle();
	await screenshot("18-conversation-b");
	console.log("• message actions: copy, read aloud, rate, retry, edit");
	const lastOf = async (id) => (await all(css(id))).at(-1);
	// A turn's actions appear when it finishes, so until then the last ones belong to the turn before.
	const finished = async () => (await all(css("turn"))).length === (await all(css("agent-actions"))).length;
	await say("Actions **test** one");
	await until("the first reply", async () => (await lastAnswer()).includes("Actions test one") && (await finished()));
	console.log("  - copy");
	// The test's display may have no clipboard to write to, so this proves a click gives an answer and no crash.
	const copyLabel = async (id) => dom(`return [...document.querySelectorAll('[data-testid="${id}"]')].at(-1).getAttribute("aria-label")`);
	for (const id of ["agent-copy", "user-copy"]) {
		await click(await lastOf(id));
		const answer = await until(`${id} to answer`, async () => {
			const label = await copyLabel(id);
			return label === "Copy" ? null : label;
		});
		console.log(`    ${id}: ${answer}`);
	}
	console.log("  - speak");
	await dom(`const speak = speechSynthesis.speak.bind(speechSynthesis); speechSynthesis.speak = (utterance) => { window.__said = utterance.text; speak(utterance); };`);
	await click(await lastOf("agent-speak"));
	await until("the words to be spoken without markup", async () => (await dom("return window.__said"))?.endsWith("Actions test one"));
	await dom("speechSynthesis.cancel()");

	console.log("  - rate");
	await click(await lastOf("agent-good"));
	const feedbackFile = join(home, ".config/steve/feedback.jsonl");
	await until("the rating to be saved", async () => existsSync(feedbackFile) && JSON.parse(readFileSync(feedbackFile, "utf8").trim().split("\n").at(-1)).rating === "good");
	await click(await lastOf("agent-bad"));
	await until("the rating to change", async () => JSON.parse(readFileSync(feedbackFile, "utf8").trim().split("\n").at(-1)).rating === "bad");
	if ((statSync(feedbackFile).mode & 0o077) !== 0) throw new Error("Feedback quotes the conversation, so it should be private");
	await screenshot("19-actions");

	console.log("  - retry");
	const turnCount = async () => (await all(css("turn"))).length;
	const before3 = await turnCount();
	await click(await lastOf("agent-retry"));
	await until("the retried reply", async () => (await turnCount()) === before3 && (await finished()) && (await lastAnswer()).includes("Actions test one"));
	await click(await lastOf("user-retry"));
	await until("the second retry", async () => (await turnCount()) === before3 && (await finished()) && (await lastAnswer()).includes("Actions test one"));

	console.log("  - edit");
	await click(await lastOf("user-edit"));
	await one(css("edit-box"));
	await wd("POST", `/session/${session}/element/${await one(css("edit-box"))}/clear`, {});
	await fill("edit-box", "Edited *words*");
	await screenshot("19-editing");
	await click(await one(css("edit-save")));
	await until("the edited reply", async () => (await lastAnswer()).includes("Edited words") && (await finished()));
	if ((await turnCount()) !== before3) throw new Error("An edit should replace its exchange");
	if ((await dom(`return [...document.querySelectorAll('[data-testid="user-message"]')].at(-1).querySelector("em")?.textContent`)) !== "words") throw new Error("An edited message should render as Markdown");

	console.log("  - retry earlier");
	await click((await all(css("user-retry")))[0]);
	await one(css("retry-dialog"));
	await screenshot("19-retry-dialog");
	await click(await one(css("retry-confirm")));
	await until("the later exchanges to go", async () => (await turnCount()) === 1);
	await until("the first exchange's reply", async () => (await all(css("agent-retry"))).length === 1);

	await click(await one(css("new-chat")));
	await settle();
	await screenshot("20-empty-b");

	console.log("• conversations come back after the app restarts");
	await click(await one(css("new-chat")));
	await say("Remember the word **HERON** for me");
	await until("the reply to save", async () => (await lastAnswer()).includes("Remember the word HERON") && (await finished()));
	const listed = await all(css("conversation"));
	if (listed.length < 2) throw new Error("This test expects an older conversation to delete");
	const titlesBefore = await Promise.all(listed.map(domTextOf));
	const doomed = await domTextOf(listed[1]);
	await dom(`document.querySelectorAll('[aria-label="Delete conversation"]')[1].click()`);
	await until("the deleted conversation to go", async () => (await all(css("conversation"))).length === listed.length - 1);
	await new Promise((resolve) => setTimeout(resolve, 500));

	await wd("DELETE", `/session/${session}`);
	session = (await wd("POST", "/session", { capabilities: { alwaysMatch: { browserName: "wry", "tauri:options": { application: app } } } })).sessionId;
	await until("the restarted app's agent", async () => (await all(css("empty-chat"))).length > 0, 60000);
	// Not a count: a conversation whose only reply was stopped was never saved.
	const savedTitles = async () => Promise.all((await all(css("conversation"))).map(domTextOf));
	await until("the saved conversation", async () => (await savedTitles()).some((title) => title.includes("HERON")), 30000).catch(async (error) => {
		console.error("Listed:", JSON.stringify(await savedTitles()), "before:", JSON.stringify(titlesBefore), "doomed:", doomed);
		throw error;
	});
	const titles = await savedTitles();
	console.log("  restored:", JSON.stringify(titles));
	if (titles.includes(doomed)) throw new Error(`A deleted conversation came back: ${doomed}`);
	const onServer = await wd("POST", `/session/${session}/execute/async`, {
		script: `const done = arguments[arguments.length - 1]; window.__TAURI_INTERNALS__.invoke("list_conversations").then((r) => done(r.data.map((c) => c.title)), (e) => done(String(e)));`,
		args: [],
	});
	if (JSON.stringify(onServer) !== JSON.stringify(titles)) throw new Error(`A retry left something saved: ${JSON.stringify(onServer)} but the app lists ${JSON.stringify(titles)}`);
	await dom(`document.querySelector('[data-testid="conversation"] button').click()`);
	await until("its messages", async () => (await dom(`return [...document.querySelectorAll('[data-testid="user-message"]')].at(-1)?.querySelector("strong")?.textContent`)) === "HERON");
	await until("its reply", async () => (await lastAnswer()).includes("Remember the word HERON"));
	await until("its actions", finished);
	await screenshot("21-restored");
	await say("And what next?");
	await until("a reply that continues it", async () => (await lastAnswer()).includes("And what next?"));
	if ((await all(css("user-message"))).length !== 2) throw new Error("The new message should follow the restored one");

	// Settings: saved to the config without touching the rest of it, and the agent comes back.
	console.log("Settings");
	await screenshot("30-conversations");
	const looks = await dom(`return [...document.querySelectorAll('[data-testid="conversation"] [data-sidebar="menu-button"]')].map((b) => { const s = getComputedStyle(b); return { active: b.hasAttribute("data-active"), fill: s.backgroundColor, border: s.borderTopWidth, radius: s.borderTopLeftRadius }; })`);
	console.log("  conversations:", JSON.stringify(looks));
	if (looks.filter((look) => look.active).length !== 1) throw new Error("Exactly one conversation should look selected");
	for (const look of looks) {
		const clear = /rgba\(0, 0, 0, 0\)|transparent/.test(look.fill);
		if (look.active === clear) throw new Error(`Only the selected conversation should be filled: ${JSON.stringify(look)}`);
		if (look.border !== "1px" || parseFloat(look.radius) < 6) throw new Error(`Conversations need a thin round border: ${JSON.stringify(look)}`);
	}
	await click(await one(css("settings-button")));
	await one(css("settings-dialog"));
	await until("the browser check", async () => (await domText(css("browser-status")))?.includes("browser"), 30000);
	await dom(`document.querySelector('[data-testid="setting-network"]').click()`);
	await fill("setting-hosts", "nas.local, 192.168.1.0/24");
	await fill("setting-search", "not a search page");
	await click(await one(css("settings-save")));
	await until("a refusal of the bad search page", async () => (await domText(css("settings-error")))?.includes("{query}"));
	await dom(`const box = document.querySelector('[data-testid="setting-search"]'); box.value = ""; box.dispatchEvent(new Event("input", { bubbles: true }));`);
	await fill("setting-search", "https://search.test/?q={query}");
	await screenshot("31-settings");
	await click(await one(css("settings-save")));
	await until("the dialog to close", async () => (await all(css("settings-dialog"))).length === 0, 60000);
	const savedConfig = readFileSync(join(home, ".config/steve/config.yaml"), "utf8");
	for (const part of ["network: true", "nas.local", "192.168.1.0/24", "search.test", "api_key: k", "model: fake"]) {
		if (!savedConfig.includes(part)) throw new Error(`The saved config is missing ${part}`);
	}
	await until("the agent after saving", async () => (await all(css("composer"))).length > 0, 60000);

	// Commands: manual shows the command and waits, and it runs in the working folder.
	console.log("Commands");
	await setMode("manual");
	await click(await one(css("new-chat")));
	await say("RUN COMMAND");
	await until("the command's approval card", async () => (await domText(css("approval-tool"))) === "execute", 60000);
	if (existsSync(join(here, "from-command.txt"))) throw new Error("A command ran before approval");
	await screenshot("32-command-approval");
	await click(await one(css("approval-allow")));
	await until("the command to run", async () => existsSync(join(here, "from-command.txt")));

	console.log("All end-to-end checks passed.");
} catch (error) {
	failed = true;
	console.error(`FAILED: ${error.message}`);
	if (session) {
		await screenshot("failure").catch(() => {});
		const script = "return JSON.stringify([...document.querySelector('textarea').attributes].map(a => a.name)) + ' | ' + document.querySelectorAll('[data-testid=composer]').length + ' | ' + document.body.innerText.slice(-200)";
		console.error(await wd("POST", `/session/${session}/execute/sync`, { script, args: [] }).catch((e) => e.message));
	}
} finally {
	if (session) await wd("DELETE", `/session/${session}`).catch(() => {});
	for (const child of processes) child.kill();
	await sleep(500);
	rmSync(home, { recursive: true, force: true });
}
process.exit(failed ? 1 : 0);
