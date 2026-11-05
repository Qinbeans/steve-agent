<script lang="ts">
	import { getContext } from "svelte";
	import { mode } from "mode-watcher";
	import { Spinner } from "$lib/components/ui/spinner";
	import { McpAppHost } from "$lib/chat/mcp-host";
	import type { ChatStore } from "$lib/chat/store.svelte";
	import type { Item } from "$lib/chat/turn";

	type Tool = Extract<Item, { kind: "tool" }>;
	let { tool }: { tool: Tool } = $props();

	const MAX_HEIGHT = 800;
	const getStore = getContext<() => ChatStore | null>("chat-store");
	const store = $derived(getStore());
	// Strings, because the tool's item is replaced as its turn progresses. An object here would look
	// new each time, re-run the effect below, and reload the page in the middle of its work.
	const server = $derived(tool.app!.server);
	const resourceUri = $derived(tool.app!.resource_uri);

	let frame = $state<HTMLIFrameElement | null>(null);
	let url = $state<string | null>(null);
	let border = $state(true);
	let height = $state(240);
	let error = $state<string | null>(null);

	function context() {
		return {
			theme: mode.current === "dark" ? "dark" : "light",
			displayMode: "inline",
			availableDisplayModes: ["inline"],
			locale: navigator.language,
			timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
			platform: "desktop",
			toolInfo: { id: tool.id, tool: { name: tool.name } },
		};
	}

	const host = new McpAppHost({
		// The page has an opaque origin, so it has no origin to name. Only this frame gets the message.
		post: (message) => frame?.contentWindow?.postMessage(message, "*"),
		callTool: (name, args) => store!.callMcpAppTool(server, name, args),
		readResource: (uri) => store!.readMcpAppResource(server, uri),
		openLink: (link) => store!.openLink(link),
		draftMessage: (text) => {
			if (store) store.draft = text;
		},
		setModelContext: (text) => store?.setAppContext(tool.id, text),
		resize: (value) => (height = Math.min(Math.max(value, 40), MAX_HEIGHT)),
		hostContext: context,
	});

	$effect(() => {
		let token: string | null = null;
		let gone = false;
		const current = store;
		if (!current) return;
		current
			.openMcpApp(server, resourceUri)
			.then((page) => {
				if (gone) return void current.closeMcpApp(page.token);
				token = page.token;
				url = page.url;
				border = page.prefersBorder !== false;
			})
			.catch((problem) => (error = String(problem)));
		return () => {
			gone = true;
			host.teardown("The tool's output was closed.");
			if (token) void current.closeMcpApp(token);
		};
	});

	$effect(() => {
		// A message's source is only this frame's window: other frames cannot speak for this app.
		const listener = (event: MessageEvent) => {
			if (frame && event.source === frame.contentWindow) void host.receive(event.data);
		};
		window.addEventListener("message", listener);
		return () => window.removeEventListener("message", listener);
	});

	$effect(() => {
		if (!tool.args) return;
		try {
			if (tool.status !== "in_progress" && tool.status !== "calling") host.setInput(JSON.parse(tool.args));
		} catch {
			host.setInput({});
		}
	});

	$effect(() => {
		if (tool.appResult) host.setResult($state.snapshot(tool.appResult));
		else if (tool.status === "failed" || tool.status === "incomplete") host.setCancelled(tool.error ?? "The tool did not finish.");
	});

	$effect(() => {
		host.contextChanged({ theme: mode.current === "dark" ? "dark" : "light" });
	});
</script>

<div class={["overflow-hidden rounded-md", border && "border"]} data-testid="mcp-app">
	{#if error}
		<p class="text-destructive p-3 text-xs" data-testid="mcp-app-error">{error}</p>
	{:else if url}
		<!-- allow-scripts alone gives the page an opaque origin: no access to this window, its storage or the app's commands. -->
		<iframe
			bind:this={frame}
			src={url}
			sandbox="allow-scripts"
			title={`${tool.name} app`}
			class="block w-full bg-transparent"
			style:height={`${height}px`}
			data-testid="mcp-app-frame"
		></iframe>
	{:else}
		<div class="flex h-24 items-center justify-center"><Spinner /></div>
	{/if}
</div>
