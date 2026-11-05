export type BackendStatus =
	| { state: "starting" }
	| { state: "ready" }
	| { state: "failed"; message: string };

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type OnEvent = (event: any) => void;

export interface WorkspaceState {
	path: string | null;
	/** Folders used before, newest first. */
	recent: string[];
}

export interface McpServer {
	name: string;
	kind: "command" | "url" | "unknown";
	target: string;
	auth: "none" | "bearer" | "oauth";
	status: "connected" | "failed" | "needs_login" | "unknown";
	tools: string[];
	error: string | null;
}

export interface NewMcpServer {
	name: string;
	command?: string;
	args?: string[];
	env?: Record<string, string>;
	url?: string;
	bearerToken?: string;
	oauth?: boolean;
}

export interface Skill {
	name: string;
	description: string;
	scope: "personal" | "project";
}

export interface Feedback {
	responseId: string | null;
	rating: "good" | "bad" | null;
	prompt: string;
	reply: string;
	turn: number;
}

export type Mode = "manual" | "auto" | "planning";

export interface SavedConversation {
	id: string;
	title: string;
	updated_at: number;
	head_response_id: string | null;
}

export interface SavedTurn {
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	response: any;
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	input_items: any[];
	compaction: boolean;
}

export interface AgentSettings {
	commandsEnabled: boolean;
	commandsNetwork: boolean;
	commandsTimeout: number;
	webEnabled: boolean;
	allowedHosts: string[];
	obeyRobots: boolean;
	browser: string;
	searchUrl: string;
}

export interface McpAppPage {
	url: string;
	token: string;
	prefersBorder: boolean | null;
}

export interface Bridge {
	getWorkspace(): Promise<WorkspaceState>;
	setWorkspace(path: string | null): Promise<BackendStatus>;
	pickFolder(): Promise<string | null>;
	pickFiles(): Promise<string[]>;
	getMcpServers(): Promise<McpServer[]>;
	addMcpServer(server: NewMcpServer): Promise<BackendStatus>;
	removeMcpServer(name: string): Promise<BackendStatus>;
	mcpLogin(name: string): Promise<BackendStatus>;
	openMcpApp(server: string, uri: string): Promise<McpAppPage>;
	closeMcpApp(token: string): Promise<void>;
	callMcpAppTool(server: string, name: string, args: unknown): Promise<unknown>;
	readMcpAppResource(server: string, uri: string): Promise<unknown>;
	listConversations(): Promise<SavedConversation[]>;
	getConversationTurns(id: string): Promise<SavedTurn[]>;
	deleteConversation(id: string): Promise<void>;
	saveFeedback(feedback: Feedback): Promise<void>;
	copyText(text: string): Promise<void>;
	getAgentSettings(): Promise<AgentSettings>;
	setAgentSettings(settings: AgentSettings): Promise<BackendStatus>;
	/** Rejects with the reason if the browser for the web tools cannot start. */
	browserStatus(): Promise<void>;
	setupBrowser(): Promise<void>;
	getSkills(): Promise<Skill[]>;
	pickSkillFolder(): Promise<string | null>;
	addSkill(path: string): Promise<string>;
	removeSkill(name: string): Promise<void>;
	onFilesDropped(listener: (paths: string[]) => void): void;
	onMenu(listener: (id: string) => void): void;
	compact(responseId: string): Promise<unknown>;
	start(): Promise<BackendStatus>;
	onStatus(listener: (status: BackendStatus) => void): void;
	send(
		input: string,
		attachments: string[],
		previousResponseId: string | null,
		streamId: string,
		mode: Mode,
		onEvent: OnEvent,
	): Promise<void>;
	answerQuestion(responseId: string, questionId: string, answer: unknown): Promise<void>;
	watch(responseId: string, streamId: string, onEvent: OnEvent): Promise<void>;
	cancel(streamId: string): Promise<void>;
	deleteResponse(responseId: string): Promise<void>;
	openExternal(url: string): Promise<void>;
}
