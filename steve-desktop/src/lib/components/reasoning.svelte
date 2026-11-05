<script lang="ts">
	import * as Collapsible from "$lib/components/ui/collapsible";
	import { buttonVariants } from "$lib/components/ui/button";
	import { Spinner } from "$lib/components/ui/spinner";
	import { cn } from "$lib/utils";
	import ChevronRightIcon from "@lucide/svelte/icons/chevron-right";
	import type { Item } from "$lib/chat/turn";

	type Reasoning = Extract<Item, { kind: "reasoning" }>;
	let { item }: { item: Reasoning } = $props();

	const thinking = $derived(item.status === "in_progress");
	let toggled = $state<boolean | null>(null);
	// Open while it streams and folded once done, until the reader chooses otherwise.
	const open = $derived(toggled ?? thinking);
</script>

<Collapsible.Root {open} onOpenChange={(value) => (toggled = value)} data-testid="reasoning">
	<Collapsible.Trigger class={cn(buttonVariants({ variant: "ghost", size: "sm" }), "text-muted-foreground")}>
		<ChevronRightIcon data-icon="inline-start" class={cn("transition-transform", open && "rotate-90")} />
		{thinking ? "Thinking" : "Thought process"}
		{#if thinking}
			<Spinner />
		{/if}
	</Collapsible.Trigger>
	<Collapsible.Content>
		<p class="text-muted-foreground mt-1 ml-3 border-l-2 pl-3 text-sm whitespace-pre-wrap" data-testid="reasoning-text">
			{item.text}
		</p>
	</Collapsible.Content>
</Collapsible.Root>
