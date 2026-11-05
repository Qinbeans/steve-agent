<script lang="ts">
	import * as Tooltip from "$lib/components/ui/tooltip";
	import { buttonVariants } from "$lib/components/ui/button";
	import { Spinner } from "$lib/components/ui/spinner";
	import { cn } from "$lib/utils";
	import type { ContextUsage } from "$lib/chat/turn";

	let {
		context,
		busy,
		onCompact,
	}: { context: ContextUsage; busy: boolean; onCompact: () => void } = $props();

	const COLORS = ["--chart-1", "--chart-2", "--chart-3", "--chart-4", "--chart-5", "--chart-6"];
	const RADIUS = 7;
	const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

	// With no known context size, the point where summarization starts is the fullest it gets.
	const capacity = $derived(context.limit ?? context.compacts_at);
	const percent = $derived(Math.round((context.used / capacity) * 100));

	const slices = $derived.by(() => {
		let offset = 0;
		return context.breakdown.map((part, index) => {
			const length = Math.min(part.tokens / capacity, 1) * CIRCUMFERENCE;
			const slice = { ...part, color: `var(${COLORS[index % COLORS.length]})`, length, offset };
			offset += length;
			return slice;
		});
	});

	function format(tokens: number): string {
		return tokens >= 1000 ? `${(tokens / 1000).toFixed(tokens >= 10000 ? 0 : 1)}k` : String(tokens);
	}
</script>

<Tooltip.Root>
	<Tooltip.Trigger
		class={cn(buttonVariants({ variant: "ghost", size: "icon-sm" }))}
		aria-label={`Context ${percent}% full. Click to compact.`}
		data-testid="context-pie"
		data-used={context.used}
		disabled={busy}
		onclick={onCompact}
	>
		{#if busy}
			<Spinner />
		{:else}
			<svg viewBox="0 0 18 18" class="-rotate-90" aria-hidden="true">
				<circle cx="9" cy="9" r={RADIUS} fill="none" stroke="var(--muted)" stroke-width="3" />
				{#each slices as slice (slice.label)}
					<circle
						cx="9"
						cy="9"
						r={RADIUS}
						fill="none"
						stroke={slice.color}
						stroke-width="3"
						stroke-dasharray={`${slice.length} ${CIRCUMFERENCE - slice.length}`}
						stroke-dashoffset={-slice.offset}
					/>
				{/each}
			</svg>
		{/if}
	</Tooltip.Trigger>
	<Tooltip.Content class="w-56">
		<div class="flex flex-col gap-1.5 py-1" data-testid="context-breakdown">
			<div class="flex justify-between gap-4 font-medium">
				<span>Context</span>
				<span>{format(context.used)} / {format(capacity)} ({percent}%)</span>
			</div>
			{#each slices as slice (slice.label)}
				<div class="flex items-center gap-2">
					<span class="size-2 shrink-0 rounded-full" style:background-color={slice.color}></span>
					<span class="flex-1">{slice.label}</span>
					<span>{format(slice.tokens)}</span>
				</div>
			{/each}
			<p class="opacity-70">Summarizes on its own at {format(context.compacts_at)}. Click to compact now.</p>
		</div>
	</Tooltip.Content>
</Tooltip.Root>
