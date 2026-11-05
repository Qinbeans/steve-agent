// Lets the UI run in a plain browser with `pnpm dev`. Never part of a production build: see index.ts.

import type { AgentSettings, Bridge, McpServer, OnEvent, Skill } from "./types";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const cancelled = new Set<string>();
const responses = new Map<string, unknown[]>();

let counter = 0;
const nextId = (prefix: string) => `${prefix}_${++counter}`;

const ANSWER = `Here is what I found.

## Summary

- Streaming works through the **same reducer** as the real app.
- Tool calls appear as collapsible rows.

\`\`\`python
print("hello from a code block")
\`\`\`

See [the docs](https://example.com) for more.`;

async function play(streamId: string, events: unknown[], onEvent: OnEvent, responseId: string, store = true) {
	for (const event of events) {
		if (cancelled.delete(streamId)) return;
		onEvent(event);
		await sleep(25);
	}
	if (store) responses.set(responseId, events);
}

function response(id: string, status: string, output: unknown[]) {
	return { id, status, output, error: null, usage: { total_tokens: 1234 } };
}

function textEvents(text: string, id = nextId("msg")) {
	const words = text.split(/(?<=\s)/);
	return {
		item: { type: "message", id, status: "completed", content: [{ type: "output_text", text }] },
		events: [
			{ type: "response.output_item.added", item: { type: "message", id, status: "in_progress", content: [] } },
			...words.map((delta) => ({ type: "response.output_text.delta", item_id: id, delta })),
			{
				type: "response.output_item.done",
				item: { type: "message", id, status: "completed", content: [{ type: "output_text", text }] },
			},
		],
	};
}

let workspace: { path: string | null; recent: string[] } = {
	path: "/home/you/project",
	recent: ["/home/you/project", "/home/you/notes"],
};
let mcpServers: McpServer[] = [
	{ name: "weather", kind: "url", target: "https://weather.example/mcp", auth: "oauth", status: "connected", tools: ["weather_forecast"], error: null },
	{ name: "broken", kind: "command", target: "broken-server", auth: "none", status: "failed", tools: [], error: "No answer after 20 seconds." },
];
let agentSettings: AgentSettings = {
	commandsEnabled: true,
	commandsNetwork: false,
	commandsTimeout: 120,
	webEnabled: true,
	allowedHosts: [],
	obeyRobots: true,
	browser: "",
	searchUrl: "",
};
let skills: Skill[] = [{ name: "web-research", description: "Researches a question across several sources.", scope: "personal" }];
const MOCK_CONTEXT = {
	used: 18400,
	limit: 262144,
	compacts_at: 222822,
	breakdown: [
		{ label: "System", tokens: 2100 },
		{ label: "Tools", tokens: 2400 },
		{ label: "User", tokens: 5300 },
		{ label: "Assistant", tokens: 4200 },
		{ label: "Reasoning", tokens: 1200 },
		{ label: "Tool results", tokens: 3200 },
	],
};

export const mockBridge: Bridge = {
	async getWorkspace() {
		return workspace;
	},
	async setWorkspace(path) {
		workspace = {
			path,
			recent: path ? [path, ...workspace.recent.filter((other) => other !== path)] : workspace.recent,
		};
		return { state: "ready" };
	},
	async pickFolder() {
		return "/home/you/another-project";
	},
	async pickFiles() {
		return ["/home/you/report.pdf"];
	},
	async getMcpServers() {
		return mcpServers;
	},
	async addMcpServer(server) {
		await sleep(300);
		mcpServers = [
			...mcpServers,
			{
				name: server.name,
				kind: server.url ? "url" : "command",
				target: server.url ?? server.command ?? "",
				auth: server.oauth ? "oauth" : server.bearerToken ? "bearer" : "none",
				status: server.oauth ? "needs_login" : "connected",
				tools: server.oauth ? [] : [`${server.name}_search`],
				error: server.oauth ? "Sign in to use this server." : null,
			},
		];
		return { state: "ready" };
	},
	async removeMcpServer(name) {
		mcpServers = mcpServers.filter((server) => server.name !== name);
		return { state: "ready" };
	},
	async mcpLogin(name) {
		await sleep(600);
		mcpServers = mcpServers.map((server) =>
			server.name === name ? { ...server, status: "connected", error: null, tools: [`${name}_search`] } : server,
		);
		return { state: "ready" };
	},
	async openMcpApp() {
		throw new Error("MCP Apps do not run in the browser preview.");
	},
	async closeMcpApp() {},
	async callMcpAppTool() {
		throw new Error("MCP Apps do not run in the browser preview.");
	},
	async readMcpAppResource() {
		throw new Error("MCP Apps do not run in the browser preview.");
	},
	async listConversations() {
		return [];
	},
	async getConversationTurns() {
		return [];
	},
	async deleteConversation() {},
	async saveFeedback() {},
	async copyText(text) {
		await navigator.clipboard.writeText(text);
	},
	async answerQuestion() {},
	async getAgentSettings() {
		return agentSettings;
	},
	async setAgentSettings(settings) {
		await sleep(300);
		agentSettings = settings;
		return { state: "ready" };
	},
	async browserStatus() {
		await sleep(100);
	},
	async setupBrowser() {
		await sleep(300);
	},
	async getSkills() {
		return skills;
	},
	async pickSkillFolder() {
		return "/home/you/downloads/summarize";
	},
	async addSkill() {
		skills = [...skills, { name: "summarize", description: "Summarizes long documents.", scope: "personal" }];
		return "summarize";
	},
	async removeSkill(name) {
		skills = skills.filter((skill) => skill.name !== name);
	},
	onFilesDropped() {},
	onMenu() {},
	async compact(responseId) {
		await sleep(400);
		return {
			...response(nextId("resp"), "completed", [textEvents("Conversation compacted.").item]),
			previous_response_id: responseId,
			context: { ...MOCK_CONTEXT, used: 6100 },
		};
	},
	async start() {
		await sleep(300);
		return { state: "ready" };
	},
	onStatus() {},
	async send(input, _attachments, _previous, streamId, _mode, onEvent) {
		const id = nextId("resp");
		if (/fail/i.test(input)) {
			throw new Error("The agent run failed.");
		}
		const childId = nextId("resp");
		const child = textEvents("The subagent looked into it and says **PONG**.");
		responses.set(childId, [
			{ type: "response.created", response: { id: childId } },
			...child.events,
			{ type: "response.completed", response: response(childId, "completed", [child.item]) },
		]);
		const tool = {
			type: "mcp_call",
			id: nextId("mcp"),
			name: "ls",
			arguments: '{"path":"/"}',
			output: "AGENTS.md\nmemories\nskills",
			status: "completed",
		};
		const task = {
			type: "mcp_call",
			id: nextId("mcp"),
			name: "task",
			arguments: '{"description":"Look into it","subagent_type":"general-purpose"}',
			output: "PONG",
			status: "completed",
			child_response_id: childId,
		};
		const answer = textEvents(ANSWER);
		await play(
			streamId,
			[
				{ type: "response.created", response: { id } },
				{ type: "response.output_item.added", item: { type: "reasoning", id: "rs_mock", status: "in_progress", summary: [] } },
				{ type: "response.reasoning_summary_text.delta", item_id: "rs_mock", delta: "Working out what to look at first." },
				{ type: "response.output_item.done", item: { type: "reasoning", id: "rs_mock", status: "completed", summary: [{ text: "Working out what to look at first." }] } },
				{ type: "response.output_item.added", item: { ...tool, status: "in_progress", output: null } },
				{ type: "response.output_item.done", item: tool },
				{ type: "response.output_item.added", item: { ...task, status: "in_progress", output: null } },
				{ type: "response.output_item.done", item: task },
				...answer.events,
				{ type: "response.completed", response: { ...response(id, "completed", [tool, task, answer.item]), context: MOCK_CONTEXT } },
			],
			onEvent,
			id,
		);
	},
	async watch(responseId, streamId, onEvent) {
		const events = responses.get(responseId);
		if (!events) throw new Error(`Response '${responseId}' was not found.`);
		await play(streamId, events, onEvent, responseId, false);
	},
	async cancel(streamId) {
		cancelled.add(streamId);
	},
	async deleteResponse(responseId) {
		responses.delete(responseId);
	},
	async openExternal(url) {
		window.open(url, "_blank", "noopener");
	},
};
