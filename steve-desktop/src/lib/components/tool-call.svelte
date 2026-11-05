<script lang="ts">
	import * as Collapsible from "$lib/components/ui/collapsible";
	import { Badge } from "$lib/components/ui/badge";
	import { Button } from "$lib/components/ui/button";
	import { Spinner } from "$lib/components/ui/spinner";
	import { buttonVariants } from "$lib/components/ui/button";
	import { cn } from "$lib/utils";
	import ChevronRightIcon from "@lucide/svelte/icons/chevron-right";
	import EyeIcon from "@lucide/svelte/icons/eye";
	import McpApp from "$lib/components/mcp-app.svelte";
	import { formatArgs, type Item } from "$lib/chat/turn";

	type Tool = Extract<Item, { kind: "tool" }>;
	let { tool, onWatch }: { tool: Tool; onWatch?: (tool: Tool) => void } = $props();

	let open = $state(false);
	const running = $derived(tool.status === "in_progress" || tool.status === "calling");
	const failed = $derived(tool.status === "failed" || tool.status === "incomplete");
	const label = $derived(tool.status === "incomplete" ? "stopped" : tool.status);
</script>

<Collapsible.Root bind:open class="rounded-md border" data-testid="tool-call" data-tool={tool.name}>
	<div class="flex items-center gap-1 pr-1">
		<Collapsible.Trigger class={cn(buttonVariants({ variant: "ghost", size: "sm" }), "min-w-0 flex-1 justify-start")}>
			<ChevronRightIcon data-icon="inline-start" class={cn("transition-transform", open && "rotate-90")} />
			<span class="truncate font-mono">{tool.name}</span>
			{#if running}
				<Spinner />
			{:else}
				<Badge variant={failed ? "destructive" : "secondary"}>{label}</Badge>
			{/if}
		</Collapsible.Trigger>
		{#if tool.childId && onWatch}
			<Button variant="outline" size="sm" data-testid="watch-subagent" onclick={() => onWatch?.(tool)}>
				<EyeIcon data-icon="inline-start" />
				Watch
			</Button>
		{/if}
	</div>
	<Collapsible.Content class="flex flex-col gap-2 border-t p-3 text-xs">
		{#if tool.args}
			<div class="flex flex-col gap-1">
				<span class="text-muted-foreground">Arguments</span>
				<pre class="bg-muted max-h-48 overflow-auto rounded-sm p-2 whitespace-pre-wrap">{formatArgs(tool.args)}</pre>
			</div>
		{/if}
		{#if tool.output}
			<div class="flex flex-col gap-1">
				<span class="text-muted-foreground">Output</span>
				<pre class="bg-muted max-h-64 overflow-auto rounded-sm p-2 whitespace-pre-wrap">{tool.output}</pre>
			</div>
		{/if}
		{#if tool.error}
			<div class="flex flex-col gap-1">
				<span class="text-destructive">Error</span>
				<pre class="bg-muted max-h-64 overflow-auto rounded-sm p-2 whitespace-pre-wrap">{tool.error}</pre>
			</div>
		{/if}
	</Collapsible.Content>
</Collapsible.Root>
{#if tool.app}
	<McpApp {tool} />
{/if}
