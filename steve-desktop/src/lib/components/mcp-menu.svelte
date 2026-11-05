<script lang="ts">
	import * as Dialog from "$lib/components/ui/dialog";
	import * as DropdownMenu from "$lib/components/ui/dropdown-menu";
	import * as Field from "$lib/components/ui/field";
	import { Badge } from "$lib/components/ui/badge";
	import { Button, buttonVariants } from "$lib/components/ui/button";
	import { Input } from "$lib/components/ui/input";
	import { Spinner } from "$lib/components/ui/spinner";
	import { Textarea } from "$lib/components/ui/textarea";
	import ChevronDownIcon from "@lucide/svelte/icons/chevron-down";
	import PlugIcon from "@lucide/svelte/icons/plug";
	import PlusIcon from "@lucide/svelte/icons/plus";
	import type { McpServer } from "$lib/bridge";
	import { parseArgs, parseEnv } from "$lib/chat/addons";
	import type { ChatStore } from "$lib/chat/store.svelte";

	let { store }: { store: ChatStore } = $props();

	let adding = $state(false);
	let viewingName = $state<string | null>(null);
	const viewing = $derived(store.mcpServers.find((server) => server.name === viewingName) ?? null);
	let busy = $state(false);
	let error = $state<string | null>(null);

	let name = $state("");
	let kind = $state<"command" | "url">("url");
	let command = $state("");
	let args = $state("");
	let env = $state("");
	let url = $state("");
	let auth = $state<"none" | "bearer" | "oauth">("none");
	let token = $state("");

	const ready = $derived(name.trim() && (kind === "command" ? command.trim() : url.trim()));
	const restartNote = $derived(
		store.replying ? "This restarts Steve, which stops the reply in progress." : null,
	);
	const problems = $derived(store.mcpServers.filter((server) => server.status === "failed" || server.status === "needs_login").length);

	const STATUS: Record<McpServer["status"], string> = {
		connected: "Connected",
		failed: "Failed",
		needs_login: "Needs sign-in",
		unknown: "Not connected",
	};

	export function openAdd() {
		name = command = args = env = url = token = "";
		kind = "url";
		auth = "none";
		error = null;
		adding = true;
	}

	async function run(action: () => Promise<void>) {
		busy = true;
		error = null;
		try {
			await action();
			return true;
		} catch (problem) {
			error = problem instanceof Error ? problem.message : String(problem);
			return false;
		} finally {
			busy = false;
		}
	}

	async function add() {
		const added = await run(() =>
			store.addMcpServer(
				kind === "command"
					? { name, command, args: parseArgs(args), env: parseEnv(env) }
					: { name, url, bearerToken: auth === "bearer" ? token : undefined, oauth: auth === "oauth" },
			),
		);
		// An OAuth server is saved before signing in, so a failed sign-in is shown on the server itself.
		if (added) adding = false;
		else if (auth === "oauth" && store.mcpServers.some((server) => server.name === name)) {
			adding = false;
			viewingName = name;
		}
	}
</script>

<DropdownMenu.Root>
	<DropdownMenu.Trigger
		class={buttonVariants({ variant: "ghost", size: "sm", class: "text-muted-foreground" })}
		data-testid="mcp-button"
	>
		<PlugIcon data-icon="inline-start" />
		MCP
		{#if problems}
			<Badge variant="destructive" data-testid="mcp-problems">{problems}</Badge>
		{:else if store.mcpServers.length}
			<span class="text-xs">{store.mcpServers.length}</span>
		{/if}
		<ChevronDownIcon data-icon="inline-end" />
	</DropdownMenu.Trigger>
	<DropdownMenu.Content side="top" align="start" class="w-80" data-testid="mcp-menu">
		{#each store.mcpServers as server (server.name)}
			<DropdownMenu.Item
				data-testid="mcp-server"
				onSelect={() => {
					error = null;
					viewingName = server.name;
				}}
			>
				<span
					class={[
						"size-2 shrink-0 rounded-full",
						server.status === "connected" ? "bg-green-500" : server.status === "unknown" ? "bg-muted-foreground" : "bg-destructive",
					]}
				></span>
				<span class="flex min-w-0 flex-1 flex-col">
					<span class="truncate">{server.name}</span>
					<span class="text-muted-foreground truncate text-xs">
						{server.status === "connected" ? `${server.tools.length} tools` : STATUS[server.status]}
					</span>
				</span>
			</DropdownMenu.Item>
		{/each}
		{#if store.mcpServers.length}
			<DropdownMenu.Separator />
		{/if}
		<DropdownMenu.Item data-testid="mcp-add" onSelect={openAdd}>
			<PlusIcon />
			Add MCP server…
		</DropdownMenu.Item>
	</DropdownMenu.Content>
</DropdownMenu.Root>

<Dialog.Root bind:open={adding}>
	<Dialog.Content data-testid="mcp-add-dialog">
		<Dialog.Header>
			<Dialog.Title>Add an MCP server</Dialog.Title>
			<Dialog.Description>Steve can use this server's tools without asking first, so only add servers you trust.</Dialog.Description>
		</Dialog.Header>
		<form
			class="contents"
			onsubmit={(event) => {
				event.preventDefault();
				void add();
			}}
		>
			<Field.Field>
				<Field.Label for="mcp-name">Name</Field.Label>
				<Input id="mcp-name" bind:value={name} autocomplete="off" spellcheck="false" placeholder="weather" data-testid="mcp-name" />
			</Field.Field>
			<div class="flex gap-1" role="group" aria-label="Type of server">
				<Button type="button" size="sm" variant={kind === "url" ? "secondary" : "ghost"} data-testid="mcp-kind-url" onclick={() => (kind = "url")}>Remote URL</Button>
				<Button type="button" size="sm" variant={kind === "command" ? "secondary" : "ghost"} data-testid="mcp-kind-command" onclick={() => (kind = "command")}>Local command</Button>
			</div>
			{#if kind === "command"}
				<Field.Field>
					<Field.Label for="mcp-command">Command</Field.Label>
					<Input id="mcp-command" bind:value={command} autocomplete="off" spellcheck="false" placeholder="uvx" data-testid="mcp-command" />
				</Field.Field>
				<Field.Field>
					<Field.Label for="mcp-args">Arguments</Field.Label>
					<Input id="mcp-args" bind:value={args} autocomplete="off" spellcheck="false" placeholder="my-server --flag" data-testid="mcp-args" />
				</Field.Field>
				<Field.Field>
					<Field.Label for="mcp-env">Environment</Field.Label>
					<Textarea id="mcp-env" bind:value={env} rows={2} spellcheck="false" placeholder="API_KEY=value" data-testid="mcp-env" />
				</Field.Field>
			{:else}
				<Field.Field>
					<Field.Label for="mcp-url">URL</Field.Label>
					<Input id="mcp-url" bind:value={url} autocomplete="off" spellcheck="false" placeholder="https://example.com/mcp" data-testid="mcp-url" />
				</Field.Field>
				<div class="flex gap-1" role="group" aria-label="Authentication">
					<Button type="button" size="sm" variant={auth === "none" ? "secondary" : "ghost"} data-testid="mcp-auth-none" onclick={() => (auth = "none")}>No sign-in</Button>
					<Button type="button" size="sm" variant={auth === "bearer" ? "secondary" : "ghost"} data-testid="mcp-auth-bearer" onclick={() => (auth = "bearer")}>Bearer token</Button>
					<Button type="button" size="sm" variant={auth === "oauth" ? "secondary" : "ghost"} data-testid="mcp-auth-oauth" onclick={() => (auth = "oauth")}>OAuth</Button>
				</div>
				{#if auth === "bearer"}
					<Field.Field>
						<Field.Label for="mcp-token">Token</Field.Label>
						<Input id="mcp-token" type="password" bind:value={token} autocomplete="off" data-testid="mcp-token" />
					</Field.Field>
				{:else if auth === "oauth"}
					<p class="text-muted-foreground text-xs">Your browser opens to sign in after you add the server.</p>
				{/if}
			{/if}
			{#if error}
				<p class="text-destructive text-xs" data-testid="mcp-error">{error}</p>
			{:else if restartNote}
				<p class="text-muted-foreground text-xs">{restartNote}</p>
			{/if}
			<Dialog.Footer>
				<Button type="submit" disabled={busy || !ready} data-testid="mcp-save">
					{#if busy}
						<Spinner data-icon="inline-start" />
					{/if}
					Add server
				</Button>
			</Dialog.Footer>
		</form>
	</Dialog.Content>
</Dialog.Root>

<Dialog.Root
	open={viewing !== null}
	onOpenChange={(value) => {
		if (!value) viewingName = null;
	}}
>
	<Dialog.Content data-testid="mcp-details">
		{#if viewing}
			<Dialog.Header>
				<Dialog.Title>{viewing.name}</Dialog.Title>
				<Dialog.Description class="break-all">{viewing.target}</Dialog.Description>
			</Dialog.Header>
			<div class="flex flex-col gap-2 text-sm">
				<p data-testid="mcp-status">{STATUS[viewing.status]}</p>
				{#if viewing.error}
					<p class="text-muted-foreground break-words text-xs" data-testid="mcp-server-error">{viewing.error}</p>
				{/if}
				{#if viewing.tools.length}
					<div class="flex flex-wrap gap-1" data-testid="mcp-tools">
						{#each viewing.tools as tool (tool)}
							<Badge variant="outline">{tool}</Badge>
						{/each}
					</div>
				{/if}
				{#if error}
					<p class="text-destructive text-xs" data-testid="mcp-error">{error}</p>
				{:else if restartNote}
					<p class="text-muted-foreground text-xs">{restartNote}</p>
				{/if}
			</div>
			<Dialog.Footer>
				{#if viewing.name !== "mcp.json"}
					<Button
						variant="ghost"
						disabled={busy}
						data-testid="mcp-remove"
						onclick={async () => {
							const name = viewing.name;
							if (await run(() => store.removeMcpServer(name))) viewingName = null;
						}}
					>
						Remove
					</Button>
				{/if}
				{#if viewing.auth === "oauth"}
					<Button
						disabled={busy}
						data-testid="mcp-login"
						onclick={() => {
							const name = viewing.name;
							void run(() => store.mcpLogin(name));
						}}
					>
						{#if busy}
							<Spinner data-icon="inline-start" />
						{/if}
						{viewing.status === "connected" ? "Sign in again" : "Sign in"}
					</Button>
				{/if}
			</Dialog.Footer>
		{/if}
	</Dialog.Content>
</Dialog.Root>
