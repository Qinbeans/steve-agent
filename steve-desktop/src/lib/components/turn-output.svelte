<script lang="ts">
	import * as Alert from "$lib/components/ui/alert";
	import { Spinner } from "$lib/components/ui/spinner";
	import CircleAlertIcon from "@lucide/svelte/icons/circle-alert";
	import Markdown from "$lib/components/markdown.svelte";
	import Reasoning from "$lib/components/reasoning.svelte";
	import ToolCall from "$lib/components/tool-call.svelte";
	import type { Item, Turn } from "$lib/chat/turn";

	type Tool = Extract<Item, { kind: "tool" }>;
	let {
		turn,
		onLink,
		onWatch,
	}: { turn: Turn; onLink: (url: string) => void; onWatch?: (tool: Tool) => void } = $props();

	const working = $derived(turn.status === "streaming");
</script>

<div class="flex flex-col gap-3">
	{#each turn.items as item (item.id)}
		{#if item.kind === "message"}
			<div data-testid="assistant-message">
				{#if item.text}
					<Markdown text={item.text} {onLink} />
				{/if}
				{#if item.refusal}
					<p class="text-muted-foreground text-sm italic">{item.refusal}</p>
				{/if}
			</div>
		{:else if item.kind === "reasoning"}
			{#if item.text}
				<Reasoning {item} />
			{/if}
		{:else}
			<ToolCall tool={item} {onWatch} />
		{/if}
	{/each}

	{#if working}
		<div class="text-muted-foreground flex items-center gap-2 text-sm" data-testid="working">
			<Spinner />
			{turn.compaction ? "Compacting the conversation" : turn.questions.length ? "Waiting for your answer" : "Working"}
		</div>
	{/if}
	{#if turn.status === "failed"}
		<Alert.Root variant="destructive" data-testid="turn-error">
			<CircleAlertIcon />
			<Alert.Title>This response failed</Alert.Title>
			<Alert.Description>{turn.error}</Alert.Description>
		</Alert.Root>
	{:else if turn.status === "incomplete"}
		<p class="text-muted-foreground text-xs">The model stopped before finishing this answer.</p>
	{:else if turn.status === "cancelled"}
		<p class="text-muted-foreground text-xs" data-testid="turn-cancelled">Stopped.</p>
	{/if}
</div>
