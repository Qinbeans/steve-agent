// The host's side of the MCP Apps protocol (spec 2026-01-26): JSON-RPC over postMessage with one
// app's page. Knows nothing about the DOM, so it can be tested without one.

const PROTOCOL_VERSION = "2026-01-26";
const METHOD_NOT_FOUND = -32601;
const FAILED = -32000;

export interface HostActions {
	/** Posts a message to the page. */
	post(message: unknown): void;
	callTool(name: string, args: unknown): Promise<unknown>;
	readResource(uri: string): Promise<unknown>;
	openLink(url: string): void | Promise<void>;
	/** Puts text in the composer. The person decides whether to send it. */
	draftMessage(text: string): void;
	setModelContext(text: string): void;
	resize(height: number): void;
	hostContext(): Record<string, unknown>;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

function textOf(blocks: Json): string {
	return (Array.isArray(blocks) ? blocks : [blocks])
		.filter((block) => block?.type === "text" && typeof block.text === "string")
		.map((block) => block.text)
		.join("\n");
}

export class McpAppHost {
	#actions: HostActions;
	#ready = false;
	#input: Json = null;
	#result: Json = null;
	#cancelled: string | null = null;
	#sentInput = false;
	#sentResult = false;
	#sentCancel = false;
	#nextId = 1;

	constructor(actions: HostActions) {
		this.#actions = actions;
	}

	setInput(args: Json) {
		this.#input = args;
		this.#flush();
	}

	setResult(result: Json) {
		this.#result = result;
		this.#flush();
	}

	setCancelled(reason: string) {
		this.#cancelled = reason;
		this.#flush();
	}

	contextChanged(context: Record<string, unknown>) {
		if (this.#ready) this.#notify("ui/notifications/host-context-changed", context);
	}

	teardown(reason: string) {
		if (this.#ready) {
			this.#actions.post({ jsonrpc: "2.0", id: `teardown-${this.#nextId++}`, method: "ui/resource-teardown", params: { reason } });
		}
	}

	/** One message from the page. */
	async receive(message: Json): Promise<void> {
		if (message?.jsonrpc !== "2.0" || typeof message.method !== "string") return;
		if (message.id === undefined) {
			this.#onNotification(message.method, message.params);
			return;
		}
		try {
			const result = await this.#onRequest(message.method, message.params ?? {});
			this.#actions.post({ jsonrpc: "2.0", id: message.id, result });
		} catch (error) {
			const code = error instanceof NotFound ? METHOD_NOT_FOUND : FAILED;
			this.#actions.post({ jsonrpc: "2.0", id: message.id, error: { code, message: error instanceof Error ? error.message : String(error) } });
		}
	}

	#notify(method: string, params: unknown) {
		this.#actions.post({ jsonrpc: "2.0", method, params });
	}

	/** Nothing goes to the page before it says it is ready, and the input always comes first. */
	#flush() {
		if (!this.#ready) return;
		if (this.#input !== null && !this.#sentInput) {
			this.#sentInput = true;
			this.#notify("ui/notifications/tool-input", { arguments: this.#input });
		}
		if (this.#result !== null && !this.#sentResult && this.#sentInput) {
			this.#sentResult = true;
			this.#notify("ui/notifications/tool-result", this.#result);
		}
		if (this.#cancelled !== null && !this.#sentCancel && !this.#sentResult) {
			this.#sentCancel = true;
			this.#notify("ui/notifications/tool-cancelled", { reason: this.#cancelled });
		}
	}

	#onNotification(method: string, params: Json) {
		if (method === "ui/notifications/initialized") {
			this.#ready = true;
			this.#flush();
		} else if (method === "ui/notifications/size-changed" && typeof params?.height === "number") {
			this.#actions.resize(params.height);
		}
	}

	async #onRequest(method: string, params: Json): Promise<unknown> {
		switch (method) {
			case "ui/initialize":
				// A page that loads again knows nothing, so it gets its input and result again.
				this.#ready = this.#sentInput = this.#sentResult = this.#sentCancel = false;
				return {
					protocolVersion: PROTOCOL_VERSION,
					hostInfo: { name: "Steve", version: "0.1.0" },
					hostCapabilities: { openLinks: {}, serverTools: {}, serverResources: {} },
					hostContext: this.#actions.hostContext(),
				};
			case "ping":
				return {};
			case "tools/call":
				return this.#actions.callTool(String(params.name), params.arguments ?? {});
			case "resources/read":
				return this.#actions.readResource(String(params.uri));
			case "ui/open-link": {
				const url = new URL(String(params.url));
				if (url.protocol !== "https:" && url.protocol !== "http:") throw new Error("Only web links can be opened.");
				await this.#actions.openLink(url.href);
				return {};
			}
			case "ui/message":
				this.#actions.draftMessage(textOf(params.content));
				return {};
			case "ui/update-model-context":
				this.#actions.setModelContext(
					[textOf(params.content ?? []), params.structuredContent ? JSON.stringify(params.structuredContent) : ""].filter(Boolean).join("\n"),
				);
				return {};
			case "ui/request-display-mode":
				return { mode: "inline" };
			default:
				throw new NotFound(`Unknown method ${method}.`);
		}
	}
}

class NotFound extends Error {}
