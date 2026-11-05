import { describe, expect, it, vi } from "vitest";
import { McpAppHost, type HostActions } from "./mcp-host";

function host(overrides: Partial<HostActions> = {}) {
	const posted: any[] = [];
	const actions: HostActions = {
		post: (message) => void posted.push(message),
		callTool: vi.fn(async () => ({ content: [] })),
		readResource: vi.fn(async () => ({ contents: [] })),
		openLink: vi.fn(),
		draftMessage: vi.fn(),
		setModelContext: vi.fn(),
		resize: vi.fn(),
		hostContext: () => ({ theme: "dark", displayMode: "inline" }),
		...overrides,
	};
	return { host: new McpAppHost(actions), posted, actions };
}

const request = (id: number, method: string, params: unknown = {}) => ({ jsonrpc: "2.0", id, method, params });
const initialized = { jsonrpc: "2.0", method: "ui/notifications/initialized" };
const methods = (posted: any[]) => posted.map((message) => message.method).filter(Boolean);

describe("McpAppHost", () => {
	it("answers ui/initialize with the protocol version and context", async () => {
		const { host: app, posted } = host();
		await app.receive(request(1, "ui/initialize"));
		expect(posted[0].id).toBe(1);
		expect(posted[0].result.protocolVersion).toBe("2026-01-26");
		expect(posted[0].result.hostContext.theme).toBe("dark");
	});

	it("sends nothing before initialized, then the input before the result", async () => {
		const { host: app, posted } = host();
		app.setResult({ content: [] });
		app.setInput({ a: 1 });
		expect(posted).toEqual([]);
		await app.receive(initialized);
		expect(methods(posted)).toEqual(["ui/notifications/tool-input", "ui/notifications/tool-result"]);
		expect(posted[0].params).toEqual({ arguments: { a: 1 } });
	});

	it("sends each only once", async () => {
		const { host: app, posted } = host();
		await app.receive(initialized);
		app.setInput({});
		app.setInput({});
		app.setResult({});
		app.setResult({});
		expect(methods(posted)).toEqual(["ui/notifications/tool-input", "ui/notifications/tool-result"]);
	});

	it("gives a page that loads again its input and result again", async () => {
		const { host: app, posted } = host();
		await app.receive(initialized);
		app.setInput({ a: 1 });
		app.setResult({ content: [] });
		posted.length = 0;
		await app.receive(request(1, "ui/initialize"));
		expect(methods(posted)).toEqual([]);
		await app.receive(initialized);
		expect(methods(posted)).toEqual(["ui/notifications/tool-input", "ui/notifications/tool-result"]);
	});

	it("reports a cancelled tool, but not after its result", async () => {
		const { host: app, posted } = host();
		await app.receive(initialized);
		app.setInput({});
		app.setCancelled("stopped");
		expect(methods(posted)).toContain("ui/notifications/tool-cancelled");

		const done = host();
		await done.host.receive(initialized);
		done.host.setInput({});
		done.host.setResult({});
		done.host.setCancelled("stopped");
		expect(methods(done.posted)).not.toContain("ui/notifications/tool-cancelled");
	});

	it("passes tool calls and resource reads along, and reports their errors", async () => {
		const { host: app, posted, actions } = host({
			callTool: vi.fn(async () => {
				throw new Error("Apps cannot call that");
			}),
		});
		await app.receive(request(2, "tools/call", { name: "x", arguments: { y: 1 } }));
		expect(actions.callTool).toHaveBeenCalledWith("x", { y: 1 });
		expect(posted[0].error).toEqual({ code: -32000, message: "Apps cannot call that" });
		await app.receive(request(3, "resources/read", { uri: "ui://a" }));
		expect(actions.readResource).toHaveBeenCalledWith("ui://a");
	});

	it("only opens web links", async () => {
		const { host: app, posted, actions } = host();
		await app.receive(request(4, "ui/open-link", { url: "file:///etc/passwd" }));
		await app.receive(request(5, "ui/open-link", { url: "javascript:alert(1)" }));
		expect(actions.openLink).not.toHaveBeenCalled();
		expect(posted.every((message) => message.error)).toBe(true);
		await app.receive(request(6, "ui/open-link", { url: "https://example.com/a" }));
		expect(actions.openLink).toHaveBeenCalledWith("https://example.com/a");
	});

	it("drafts a message instead of sending it, and keeps model context as text", async () => {
		const { host: app, actions } = host();
		await app.receive(request(7, "ui/message", { role: "user", content: { type: "text", text: "hello" } }));
		expect(actions.draftMessage).toHaveBeenCalledWith("hello");
		await app.receive(request(8, "ui/update-model-context", { content: [{ type: "text", text: "seen" }], structuredContent: { a: 1 } }));
		expect(actions.setModelContext).toHaveBeenCalledWith('seen\n{"a":1}');
	});

	it("stays inline, follows size changes, and refuses unknown methods", async () => {
		const { host: app, posted, actions } = host();
		await app.receive(request(9, "ui/request-display-mode", { mode: "fullscreen" }));
		expect(posted[0].result).toEqual({ mode: "inline" });
		await app.receive({ jsonrpc: "2.0", method: "ui/notifications/size-changed", params: { width: 10, height: 321 } });
		expect(actions.resize).toHaveBeenCalledWith(321);
		await app.receive(request(10, "sampling/createMessage"));
		expect(posted.at(-1).error.code).toBe(-32601);
	});

	it("ignores anything that is not JSON-RPC", async () => {
		const { host: app, posted } = host();
		await app.receive("hello");
		await app.receive({ method: "ui/initialize", id: 1 });
		expect(posted).toEqual([]);
	});
});
