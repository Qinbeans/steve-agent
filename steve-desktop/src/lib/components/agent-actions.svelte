<script lang="ts">
	import RotateCwIcon from "@lucide/svelte/icons/rotate-cw";
	import SquareIcon from "@lucide/svelte/icons/square";
	import ThumbsDownIcon from "@lucide/svelte/icons/thumbs-down";
	import ThumbsUpIcon from "@lucide/svelte/icons/thumbs-up";
	import Volume2Icon from "@lucide/svelte/icons/volume-2";
	import ActionButton from "$lib/components/action-button.svelte";
	import CopyButton from "$lib/components/copy-button.svelte";
	import { speech } from "$lib/chat/speech.svelte";
	import { turnText, type Rating, type Turn } from "$lib/chat/turn";

	let {
		turn,
		busy,
		canRetry,
		onRate,
		onRetry,
	}: { turn: Turn; busy: boolean; canRetry: boolean; onRate: (rating: Rating) => void; onRetry: () => void } = $props();

	const text = $derived(turnText(turn));
	const id = $derived(turn.responseId ?? "");
	const speaking = $derived(speech.speakingId === id);
</script>

<div class="flex items-center gap-0.5" data-testid="agent-actions">
	{#if text}
		<CopyButton {text} testid="agent-copy" />
		{#if speech.available}
			<ActionButton label={speaking ? "Stop reading" : "Read aloud"} testid="agent-speak" pressed={speaking} onclick={() => speech.toggle(id, text)}>
				{#if speaking}
					<SquareIcon />
				{:else}
					<Volume2Icon />
				{/if}
			</ActionButton>
		{/if}
		<ActionButton label="Good response" testid="agent-good" pressed={turn.rating === "good"} onclick={() => onRate("good")}>
			<ThumbsUpIcon class={turn.rating === "good" ? "fill-current" : ""} />
		</ActionButton>
		<ActionButton label="Bad response" testid="agent-bad" pressed={turn.rating === "bad"} onclick={() => onRate("bad")}>
			<ThumbsDownIcon class={turn.rating === "bad" ? "fill-current" : ""} />
		</ActionButton>
	{/if}
	<ActionButton label={canRetry ? "Retry" : "Attach the files again to retry"} testid="agent-retry" disabled={busy || !canRetry} onclick={onRetry}>
		<RotateCwIcon />
	</ActionButton>
</div>
