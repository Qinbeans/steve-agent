import { Channel, invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { writeText } from "@tauri-apps/plugin-clipboard-manager";
import { open } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";
import type { AgentSettings, BackendStatus, Bridge, McpAppPage, McpServer, SavedConversation, SavedTurn, OnEvent, Skill, WorkspaceState } from "./types";

function channel(onEvent: OnEvent) {
	const events = new Channel<unknown>();
	events.onmessage = onEvent;
	return events;
}

export const tauriBridge: Bridge = {
	getWorkspace: () => invoke<WorkspaceState>("get_workspace"),
	setWorkspace: (path) => invoke<BackendStatus>("set_workspace", { path }),
	pickFolder: async () => (await open({ directory: true, title: "Choose a working folder" })) as string | null,
	pickFiles: async () => ((await open({ multiple: true, title: "Attach files" })) as string[] | null) ?? [],
	getMcpServers: () => invoke<McpServer[]>("get_mcp_servers"),
	addMcpServer: (server) => invoke<BackendStatus>("add_mcp_server", { server }),
	removeMcpServer: (name) => invoke<BackendStatus>("remove_mcp_server", { name }),
	mcpLogin: (name) => invoke<BackendStatus>("mcp_login", { name }),
	getAgentSettings: () => invoke<AgentSettings>("get_agent_settings"),
	setAgentSettings: (settings) => invoke<BackendStatus>("set_agent_settings", { settings }),
	browserStatus: () => invoke("browser_status"),
	setupBrowser: () => invoke("setup_browser"),
	openMcpApp: (server, uri) => invoke<McpAppPage>("mcp_app_open", { server, uri }),
	closeMcpApp: (token) => invoke("mcp_app_close", { token }),
	callMcpAppTool: (server, name, args) => invoke("mcp_app_call", { server, name, arguments: args }),
	readMcpAppResource: (server, uri) => invoke("mcp_app_read", { server, uri }),
	// Not navigator.clipboard, whose route through GTK has crashed the whole app on Wayland.
	copyText: (text) => writeText(text),
	listConversations: async () => (await invoke<{ data: SavedConversation[] }>("list_conversations")).data,
	getConversationTurns: async (id) => (await invoke<{ turns: SavedTurn[] }>("get_conversation", { id })).turns,
	deleteConversation: (id) => invoke("delete_conversation", { id }),
	saveFeedback: (feedback) => invoke("save_feedback", { feedback }),
	getSkills: () => invoke<Skill[]>("get_skills"),
	pickSkillFolder: async () => (await open({ directory: true, title: "Choose a skill's folder" })) as string | null,
	addSkill: (path) => invoke<string>("add_skill", { path }),
	removeSkill: (name) => invoke("remove_skill", { name }),
	onFilesDropped(listener) {
		void getCurrentWebview().onDragDropEvent((event) => {
			if (event.payload.type === "drop") listener(event.payload.paths);
		});
	},
	onMenu(listener) {
		void listen<string>("menu", (event) => listener(event.payload));
	},
	compact: (responseId) => invoke("compact_response", { responseId }),
	start: () => invoke<BackendStatus>("start_backend"),
	onStatus(listener) {
		void listen<BackendStatus>("backend-status", (event) => listener(event.payload));
	},
	send: (input, attachments, previousResponseId, streamId, mode, onEvent) =>
		invoke("send_message", { input, attachments, previousResponseId, streamId, mode, onEvent: channel(onEvent) }),
	answerQuestion: (responseId, questionId, answer) => invoke("answer_question", { responseId, questionId, answer }),
	watch: (responseId, streamId, onEvent) =>
		invoke("watch_response", { responseId, streamId, onEvent: channel(onEvent) }),
	cancel: (streamId) => invoke("cancel_stream", { streamId }),
	deleteResponse: (responseId) => invoke("delete_response", { responseId }),
	openExternal: (url) => openUrl(url),
};
