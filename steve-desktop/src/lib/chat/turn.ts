// Folds Responses API stream events into what the UI shows. Pure, so it can be tested without an app.

export type ItemStatus = "in_progress" | "completed" | "incomplete" | "failed" | "calling";

export type Item =
	| { kind: "message"; id: string; text: string; refusal: string; status: ItemStatus }
	| { kind: "reasoning"; id: string; text: string; status: ItemStatus }
	| {
			kind: "tool";
			id: string;
			name: string;
			args: string;
			output: string | null;
			error: string | null;
			status: ItemStatus;
			childId: string | null;
			/** The MCP App this tool opens, and the result to give it. */
			app: { server: string; resource_uri: string } | null;
			appResult: unknown;
	  };

export type Rating = "good" | "bad";

/** Something the run is waiting on the person for: approving a tool call, or an MCP elicitation. */
export interface Question {
	id: string;
	kind: "approval" | "elicitation";
	answered: boolean;
	tool?: string;
	arguments?: unknown;
	server?: string;
	mode?: "form" | "url";
	message?: string;
	schema?: unknown;
	url?: string;
}

export type TurnStatus = "streaming" | "completed" | "incomplete" | "failed" | "cancelled";

export interface ContextUsage {
	used: number;
	limit: number | null;
	compacts_at: number;
	breakdown: { label: string; tokens: number }[];
}

export interface Turn {
	input: string;
	/** Names to show. */
	attachments: string[];
	/** Where they are, so that a retry can send them again. */
	attachmentPaths: string[];
	rating: Rating | null;
	questions: Question[];
	compaction: boolean;
	context: ContextUsage | null;
	responseId: string | null;
	status: TurnStatus;
	items: Item[];
	error: string | null;
	totalTokens: number | null;
}

export function newTurn(input: string, attachments: string[] = [], compaction = false, attachmentPaths: string[] = []): Turn {
	return { input, attachments, attachmentPaths, rating: null, questions: [], compaction, context: null, responseId: null, status: "streaming", items: [], error: null, totalTokens: null };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

function errorText(error: Json): string | null {
	if (!error) return null;
	if (typeof error === "string") return error;
	const parts = Array.isArray(error.content) ? error.content : [];
	return parts.map((part: Json) => part.text ?? "").join("") || error.message || "The tool failed.";
}

function toItem(raw: Json): Item | null {
	switch (raw.type) {
		case "message":
			return {
				kind: "message",
				id: raw.id,
				text: (raw.content ?? []).map((part: Json) => part.text ?? "").join(""),
				refusal: (raw.content ?? []).map((part: Json) => part.refusal ?? "").join(""),
				status: raw.status,
			};
		case "reasoning":
			return {
				kind: "reasoning",
				id: raw.id,
				text: (raw.summary ?? []).map((part: Json) => part.text ?? "").join("\n\n"),
				status: raw.status ?? "completed",
			};
		case "mcp_call":
			return {
				kind: "tool",
				id: raw.id,
				name: raw.name,
				args: raw.arguments ?? "",
				output: raw.output ?? null,
				error: errorText(raw.error),
				status: raw.status ?? "in_progress",
				childId: raw.child_response_id ?? null,
				app: raw.app ?? null,
				appResult: raw.app_result ?? null,
			};
		default:
			return null;
	}
}

function find(turn: Turn, id: string): Item | undefined {
	return turn.items.find((item) => item.id === id);
}

/** Applies one event. The terminal event's output replaces the items: partial output from a retried attempt is not in it. */
export function applyEvent(turn: Turn, event: Json): void {
	switch (event.type) {
		case "response.created":
			turn.responseId = event.response.id;
			break;
		case "steve.approval.requested":
		case "steve.elicitation.requested": {
			const { type, sequence_number: _sequence, ...details } = event;
			turn.questions.push({ ...details, kind: type === "steve.approval.requested" ? "approval" : "elicitation", answered: false });
			break;
		}
		case "steve.interaction.resolved":
			turn.questions = turn.questions.filter((question) => question.id !== event.id);
			break;
		case "response.output_item.added": {
			const item = toItem(event.item);
			if (item) turn.items.push(item);
			break;
		}
		case "response.output_item.done": {
			const item = toItem(event.item);
			const index = turn.items.findIndex((existing) => existing.id === event.item.id);
			if (item && index >= 0) turn.items[index] = item;
			break;
		}
		case "response.output_text.delta": {
			const item = find(turn, event.item_id);
			if (item?.kind === "message") item.text += event.delta;
			break;
		}
		case "response.refusal.delta": {
			const item = find(turn, event.item_id);
			if (item?.kind === "message") item.refusal += event.delta;
			break;
		}
		case "response.reasoning_summary_text.delta": {
			const item = find(turn, event.item_id);
			if (item?.kind === "reasoning") item.text += event.delta;
			break;
		}
		case "response.mcp_call_arguments.delta": {
			const item = find(turn, event.item_id);
			if (item?.kind === "tool") item.args += event.delta;
			break;
		}
		case "response.completed":
		case "response.incomplete":
		case "response.failed": {
			const response = event.response;
			turn.responseId = response.id;
			turn.status = response.status;
			turn.error = response.error?.message ?? null;
			turn.totalTokens = response.usage?.total_tokens ?? null;
			turn.context = response.context ?? null;
			turn.items = (response.output ?? []).map(toItem).filter(Boolean) as Item[];
			turn.questions = [];
			break;
		}
	}
}

/** The stream ended. If no terminal event came, say what happened instead of showing a spinner forever. */
export function endStream(turn: Turn, cancelled: boolean, error: string | null = null): void {
	turn.questions = [];
	if (turn.status !== "streaming") return;
	if (cancelled) {
		turn.status = "cancelled";
	} else {
		turn.status = "failed";
		turn.error = error ?? "The connection to the agent ended before the response finished.";
	}
	for (const item of turn.items) {
		if (item.status === "in_progress" || item.status === "calling") item.status = "incomplete";
	}
}

/** Only finished turns can be continued. A failed or cancelled turn's input would otherwise stay in the history. */
/** A turn saved by the server, in the shape a finished turn has here. Its files' paths were never saved. */
export function restoredTurn(saved: { response: Json; input_items: Json[]; compaction: boolean }): Turn {
	const parts = saved.input_items.flatMap((item) => (Array.isArray(item.content) ? item.content : []));
	const input = parts.filter((part: Json) => part.type === "input_text").map((part: Json) => part.text).join("\n");
	const files = parts.filter((part: Json) => part.type === "input_file").map((part: Json) => part.filename ?? "file");
	const turn = newTurn(input, files, saved.compaction);
	turn.responseId = saved.response.id;
	applyEvent(turn, { type: `response.${saved.response.status}`, response: saved.response });
	return turn;
}

/** A retry sends the files again, so it needs to know where they are. */
export function canRetry(turn: Turn): boolean {
	return turn.attachments.length === 0 || turn.attachmentPaths.length > 0;
}

/** The agent's words in a turn, as the Markdown it wrote. */
export function turnText(turn: Turn): string {
	return turn.items
		.flatMap((item) => (item.kind === "message" && item.text ? [item.text] : []))
		.join("\n\n");
}

export function continuationId(turns: Turn[]): string | null {
	for (let index = turns.length - 1; index >= 0; index--) {
		const turn = turns[index];
		if ((turn.status === "completed" || turn.status === "incomplete") && turn.responseId) return turn.responseId;
	}
	return null;
}

/** What the conversation's next prompt looks like, from the latest turn that reported it. */
export function latestContext(turns: Turn[]): ContextUsage | null {
	for (let index = turns.length - 1; index >= 0; index--) {
		if (turns[index].context) return turns[index].context;
	}
	return null;
}

export function formatArgs(args: string): string {
	try {
		return JSON.stringify(JSON.parse(args), null, 2);
	} catch {
		return args;
	}
}
