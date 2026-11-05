<script lang="ts">
	import { untrack } from "svelte";
	import * as Sheet from "$lib/components/ui/sheet";
	import { Badge } from "$lib/components/ui/badge";
	import TurnOutput from "$lib/components/turn-output.svelte";
	import { newTurn, type Item, type Turn } from "$lib/chat/turn";
	import type { ChatStore } from "$lib/chat/store.svelte";

	type Tool = Extract<Item, { kind: "tool" }>;
	let {
		store,
		tool = $bindable(),
		onLink,
	}: { store: ChatStore; tool: Tool | null; onLink: (url: string) => void } = $props();

	let turn = $state<Turn>(newTurn(""));

	const description = $derived.by(() => {
		try {
			return JSON.parse(tool?.args ?? "").description ?? "";
		} catch {
			return "";
		}
	});

	$effect(() => {
		const childId = tool?.childId;
		if (!childId) return;
		// Untracked: writing turn here must not make this effect depend on it.
		return untrack(() => {
			turn = newTurn("");
			return store.watch(childId, turn);
		});
	});
</script>

<Sheet.Root
	open={tool !== null}
	onOpenChange={(open) => {
		if (!open) tool = null;
	}}
>
	<Sheet.Content class="flex flex-col gap-0 sm:max-w-xl" data-testid="subagent-sheet">
		<Sheet.Header>
			<Sheet.Title class="flex items-center gap-2">
				Subagent
				<Badge variant="secondary" data-testid="subagent-status">{turn.status}</Badge>
			</Sheet.Title>
			<Sheet.Description class="line-clamp-3">{description}</Sheet.Description>
		</Sheet.Header>
		<div class="min-h-0 flex-1 overflow-y-auto p-4">
			<TurnOutput {turn} {onLink} />
		</div>
	</Sheet.Content>
</Sheet.Root>
