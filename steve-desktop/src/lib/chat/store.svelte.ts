import type { AgentSettings, BackendStatus, Bridge, McpServer, Mode, NewMcpServer, SavedConversation, Skill, WorkspaceState } from "$lib/bridge";
import { applyEvent, continuationId, endStream, latestContext, newTurn, restoredTurn, turnText, type Question, type Rating, type Turn } from "./turn";

export interface Conversation {
	id: string;
	/** The server's id, once it has given one. A saved conversation's `id` is the same. */
	serverId: string | null;
	title: string;
	turns: Turn[];
	streamId: string | null;
	/** False for a saved conversation until it is first opened. */
	loaded: boolean;
}

function errorMessage(error: unknown): string {
	return typeof error === "string" ? error : error instanceof Error ? error.message : "Something went wrong.";
}

export function fileName(path: string): string {
	return path.split(/[\\/]/).pop() ?? path;
}

const DELETE_ATTEMPTS = 10;
const DELETE_RETRY_MS = 300;

const MODE_KEY = "steve.mode";

function loadMode(): Mode {
	try {
		const saved = localStorage.getItem(MODE_KEY);
		if (saved === "manual" || saved === "auto" || saved === "planning") return saved;
	} catch {
		// Only a convenience.
	}
	return "manual";
}

export class ChatStore {
	status = $state<BackendStatus>({ state: "starting" });
	conversations = $state<Conversation[]>([]);
	activeId = $state<string | null>(null);
	active = $derived(this.conversations.find((conversation) => conversation.id === this.activeId) ?? null);
	context = $derived(this.active ? latestContext(this.active.turns) : null);
	workspace = $state<WorkspaceState>({ path: null, recent: [] });
	/** Files waiting to go with the next message, as paths. */
	pending = $state<string[]>([]);
	mcpServers = $state<McpServer[]>([]);
	skills = $state<Skill[]>([]);
	addonsLoaded = $state(false);
	/** Starts careful: a mode that changes things without asking is one to choose. */
	mode = $state<Mode>(loadMode());
	/** Text for the composer, from an MCP App. Never sent without the person pressing send. */
	draft = $state<string | null>(null);
	#appContext = new Map<string, Map<string, string>>();

	#bridge: Bridge;

	constructor(bridge: Bridge) {
		this.#bridge = bridge;
		bridge.onStatus((status) => (this.status = status));
		bridge.onFilesDropped((paths) => this.attach(paths));
	}

	async load() {
		try {
			this.workspace = await this.#bridge.getWorkspace();
			// Before the start too, so the splash can say what the wait is for.
			await this.refreshAddons();
			this.addonsLoaded = true;
		} catch (error) {
			this.status = { state: "failed", message: errorMessage(error) };
			return;
		}
		await this.start();
		await this.refreshAddons();
		await this.#refreshConversations();
	}

	/** Whether a reply is on its way anywhere, which a restart would stop. */
	get replying() {
		return this.conversations.some((conversation) => conversation.streamId);
	}

	/** Fills the sidebar from what is saved for this folder, keeping what is already open and known. */
	async #refreshConversations() {
		let saved: SavedConversation[];
		try {
			saved = await this.#bridge.listConversations();
		} catch {
			saved = [];
		}
		const known = new Map(this.conversations.flatMap((conversation) => (conversation.serverId ? [[conversation.serverId, conversation]] : [])));
		this.conversations = saved.map(
			(entry) => known.get(entry.id) ?? { id: entry.id, serverId: entry.id, title: entry.title, turns: [], streamId: null, loaded: false },
		);
		if (!this.conversations.some((conversation) => conversation.id === this.activeId)) this.activeId = null;
	}

	/** Best effort: the menus can show nothing rather than stop the app. */
	async refreshAddons() {
		const [servers, skills] = await Promise.allSettled([this.#bridge.getMcpServers(), this.#bridge.getSkills()]);
		if (servers.status === "fulfilled") this.mcpServers = servers.value;
		if (skills.status === "fulfilled") this.skills = skills.value;
	}

	/** After anything that restarts the agent. The server keeps conversations in memory. */
	async #restarted(status: BackendStatus) {
		this.status = status;
		await this.refreshAddons();
		await this.#refreshConversations();
	}

	async addMcpServer(server: NewMcpServer) {
		await this.#restarted(await this.#bridge.addMcpServer(server));
		if (server.oauth) await this.mcpLogin(server.name);
	}

	async removeMcpServer(name: string) {
		await this.#restarted(await this.#bridge.removeMcpServer(name));
	}

	async mcpLogin(name: string) {
		await this.#restarted(await this.#bridge.mcpLogin(name));
	}

	getAgentSettings() {
		return this.#bridge.getAgentSettings();
	}

	async setAgentSettings(settings: AgentSettings) {
		await this.#restarted(await this.#bridge.setAgentSettings(settings));
	}

	browserStatus() {
		return this.#bridge.browserStatus();
	}

	setupBrowser() {
		return this.#bridge.setupBrowser();
	}

	async addSkillFromFolder() {
		const path = await this.#bridge.pickSkillFolder();
		if (!path) return;
		await this.#bridge.addSkill(path);
		await this.refreshAddons();
	}

	async removeSkill(name: string) {
		await this.#bridge.removeSkill(name);
		await this.refreshAddons();
	}

	openMcpApp = (server: string, uri: string) => this.#bridge.openMcpApp(server, uri);
	closeMcpApp = (token: string) => this.#bridge.closeMcpApp(token);
	callMcpAppTool = (server: string, name: string, args: unknown) => this.#bridge.callMcpAppTool(server, name, args);
	readMcpAppResource = (server: string, uri: string) => this.#bridge.readMcpAppResource(server, uri);
	openLink = (url: string) => this.#bridge.openExternal(url);

	/** What an app wants the model to know. Goes with the next message, replacing the app's earlier text. */
	setAppContext(appId: string, text: string) {
		if (!this.activeId) return;
		const contexts = this.#appContext.get(this.activeId) ?? new Map<string, string>();
		contexts.set(appId, text);
		this.#appContext.set(this.activeId, contexts);
	}

	#takeAppContext(conversationId: string): string {
		const contexts = this.#appContext.get(conversationId);
		this.#appContext.delete(conversationId);
		if (!contexts?.size) return "";
		return `[Context from open MCP apps]\n${[...contexts.values()].join("\n")}\n\n`;
	}

	setMode(mode: Mode) {
		this.mode = mode;
		try {
			localStorage.setItem(MODE_KEY, mode);
		} catch {
			// Only a convenience.
		}
	}

	/** A question stays open until the server says it is resolved, and an answer can only be given once. */
	async answerQuestion(turn: Turn, question: Question, answer: unknown) {
		if (!turn.responseId || question.answered) return;
		question.answered = true;
		try {
			await this.#bridge.answerQuestion(turn.responseId, question.id, answer);
		} catch {
			// It has already closed, or the run ended. Either way there is nothing left to answer.
			turn.questions = turn.questions.filter((other) => other.id !== question.id);
		}
	}

	copyText = (text: string) => this.#bridge.copyText(text);

	pickFolder = () => this.#bridge.pickFolder();

	/** Moves the agent to a folder. Throws a message to show if the folder is not usable. */
	async setWorkspace(path: string | null) {
		const status = await this.#bridge.setWorkspace(path);
		this.workspace = await this.#bridge.getWorkspace();
		await this.#restarted(status);
	}

	attach(paths: string[]) {
		this.pending = [...new Set([...this.pending, ...paths])];
	}

	detach(path: string) {
		this.pending = this.pending.filter((candidate) => candidate !== path);
	}

	async pickFiles() {
		this.attach(await this.#bridge.pickFiles());
	}

	async start() {
		this.status = { state: "starting" };
		try {
			this.status = await this.#bridge.start();
		} catch (error) {
			this.status = { state: "failed", message: errorMessage(error) };
		}
	}

	newConversation() {
		this.activeId = null;
	}

	async select(id: string) {
		this.activeId = id;
		const conversation = this.conversations.find((candidate) => candidate.id === id);
		if (!conversation || conversation.loaded || !conversation.serverId) return;
		try {
			const saved = await this.#bridge.getConversationTurns(conversation.serverId);
			conversation.turns = saved.map(restoredTurn);
			conversation.loaded = true;
		} catch (error) {
			const turn = newTurn("");
			endStream(turn, false, errorMessage(error));
			conversation.turns = [turn];
		}
	}

	async send(input: string) {
		let conversation = this.active;
		if (!conversation) {
			const id = crypto.randomUUID();
			const title = input || fileName(this.pending[0] ?? "") || "Attachment";
			this.conversations.unshift({ id, serverId: null, title: title.slice(0, 60), turns: [], streamId: null, loaded: true });
			this.activeId = id;
			conversation = this.conversations[0];
		}
		if (conversation.streamId) return;
		const attachments = this.pending;
		this.pending = [];
		await this.#sendTurn(conversation, input, attachments);
	}

	/**
	 * Sends a turn again, with new text if it was edited, and drops it and everything after it.
	 * The server continues from the turn before, so what was dropped no longer counts as context.
	 */
	async retry(conversation: Conversation, index: number, input?: string) {
		if (conversation.streamId) return;
		const turn = conversation.turns[index];
		if (!turn || turn.compaction) return;
		const dropped = conversation.turns.splice(index);
		if (index === 0 && input) conversation.title = input.slice(0, 60);
		// Dropped means gone from disk too. Retrying the first message starts a new thread, and this
		// also empties the old one, which would otherwise stay saved under the same title.
		await Promise.allSettled(dropped.flatMap((old) => (old.responseId ? [this.#bridge.deleteResponse(old.responseId)] : [])));
		if (index === 0) conversation.serverId = null;
		await this.#sendTurn(conversation, input ?? turn.input, turn.attachmentPaths);
	}

	/** Pressing the same rating again takes it back. Kept on this computer only. */
	async rate(conversation: Conversation, turn: Turn, rating: Rating) {
		const index = conversation.turns.indexOf(turn);
		turn.rating = turn.rating === rating ? null : rating;
		await this.#bridge.saveFeedback({
			responseId: turn.responseId,
			rating: turn.rating,
			prompt: turn.input,
			reply: turnText(turn),
			turn: index,
		});
	}

	async #sendTurn(conversation: Conversation, input: string, attachments: string[]) {
		const previous = continuationId(conversation.turns);
		conversation.turns.push(newTurn(input, attachments.map(fileName), false, attachments));
		// Through the store's proxy, so that changes to the turn update the UI.
		const turn = conversation.turns[conversation.turns.length - 1];
		const streamId = crypto.randomUUID();
		conversation.streamId = streamId;
		let cancelled = false;
		this.#cancels.set(streamId, () => (cancelled = true));
		try {
			await this.#bridge.send(this.#takeAppContext(conversation.id) + input, attachments, previous, streamId, this.mode, (event) => {
				if (event.type === "response.created" && event.response?.conversation?.id) conversation.serverId = event.response.conversation.id;
				applyEvent(turn, event);
			});
			endStream(turn, cancelled);
		} catch (error) {
			endStream(turn, cancelled, errorMessage(error));
		} finally {
			this.#cancels.delete(streamId);
			conversation.streamId = null;
		}
	}

	#cancels = new Map<string, () => void>();

	/** Summarizes older messages. The result is a turn of its own, which later messages continue from. */
	async compact(conversation: Conversation) {
		const previous = continuationId(conversation.turns);
		if (conversation.streamId || !previous) return;
		conversation.turns.push(newTurn("", [], true));
		const turn = conversation.turns[conversation.turns.length - 1];
		conversation.streamId = "compacting";
		try {
			const response = await this.#bridge.compact(previous);
			applyEvent(turn, { type: "response.completed", response });
		} catch (error) {
			endStream(turn, false, errorMessage(error));
		} finally {
			conversation.streamId = null;
		}
	}

	async stop(conversation: Conversation) {
		if (!conversation.streamId) return;
		this.#cancels.get(conversation.streamId)?.();
		await this.#bridge.cancel(conversation.streamId);
	}

	async remove(id: string) {
		const conversation = this.conversations.find((candidate) => candidate.id === id);
		if (!conversation) return;
		await this.stop(conversation);
		this.conversations = this.conversations.filter((candidate) => candidate.id !== id);
		if (this.activeId === id) this.activeId = null;
		if (!conversation.serverId) return;
		// A stopped run can take a moment to end, and the server refuses to delete it until then.
		for (let attempt = 0; attempt < DELETE_ATTEMPTS; attempt++) {
			try {
				await this.#bridge.deleteConversation(conversation.serverId);
				return;
			} catch {
				await new Promise((resolve) => setTimeout(resolve, DELETE_RETRY_MS));
			}
		}
	}

	/** Follows a subagent's response. Returns a function that stops watching. */
	watch(responseId: string, turn: Turn): () => void {
		const streamId = crypto.randomUUID();
		let stopped = false;
		this.#bridge
			.watch(responseId, streamId, (event) => applyEvent(turn, event))
			.then(() => endStream(turn, stopped))
			.catch((error) => endStream(turn, stopped, errorMessage(error)));
		return () => {
			stopped = true;
			void this.#bridge.cancel(streamId);
		};
	}
}
