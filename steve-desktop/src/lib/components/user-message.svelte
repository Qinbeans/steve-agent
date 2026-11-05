<script lang="ts">
	import { tick } from "svelte";
	import { Badge } from "$lib/components/ui/badge";
	import { Button } from "$lib/components/ui/button";
	import { Textarea } from "$lib/components/ui/textarea";
	import PaperclipIcon from "@lucide/svelte/icons/paperclip";
	import PencilIcon from "@lucide/svelte/icons/pencil";
	import RotateCwIcon from "@lucide/svelte/icons/rotate-cw";
	import ActionButton from "$lib/components/action-button.svelte";
	import CopyButton from "$lib/components/copy-button.svelte";
	import Markdown from "$lib/components/markdown.svelte";
	import type { Turn } from "$lib/chat/turn";

	let {
		turn,
		busy,
		actionsClass,
		canRetry,
		onLink,
		onRetry,
		onEdit,
	}: {
		turn: Turn;
		busy: boolean;
		actionsClass: string;
		canRetry: boolean;
		onLink: (url: string) => void;
		onRetry: () => void;
		onEdit: (text: string) => void;
	} = $props();

	let editing = $state(false);
	let draft = $state("");
	const ready = $derived((draft.trim().length > 0 || turn.attachments.length > 0) && !busy);

	async function startEditing() {
		draft = turn.input;
		editing = true;
		await tick();
		document.querySelector<HTMLTextAreaElement>('[data-testid="edit-box"]')?.focus();
	}

	function save() {
		if (!ready) return;
		editing = false;
		onEdit(draft.trim());
	}
</script>

<div class="flex flex-col items-end gap-1">
	{#if turn.attachments.length}
		<div class="flex flex-wrap justify-end gap-1" data-testid="sent-attachments">
			{#each turn.attachments as name, attachment (attachment)}
				<Badge variant="outline"><PaperclipIcon />{name}</Badge>
			{/each}
		</div>
	{/if}
	{#if editing}
		<div class="flex w-full flex-col gap-2">
			<Textarea
				bind:value={draft}
				rows={3}
				aria-label="Edit message"
				data-testid="edit-box"
				onkeydown={(event: KeyboardEvent) => {
					if (event.key === "Escape") editing = false;
					else if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
						event.preventDefault();
						save();
					}
				}}
			/>
			<div class="flex justify-end gap-2">
				<Button variant="ghost" size="sm" data-testid="edit-cancel" onclick={() => (editing = false)}>Cancel</Button>
				<Button size="sm" disabled={!ready} data-testid="edit-save" onclick={save}>Send</Button>
			</div>
		</div>
	{:else}
		{#if turn.input}
			<div class="bg-muted max-w-full min-w-0 rounded-lg px-3 py-2 text-sm" data-testid="user-message">
				<Markdown text={turn.input} {onLink} breaks />
			</div>
		{/if}
		<div class={["flex items-center gap-0.5", actionsClass]} data-testid="user-actions">
			<ActionButton label={canRetry ? "Retry" : "Attach the files again to retry"} testid="user-retry" disabled={busy || !canRetry} onclick={onRetry}>
				<RotateCwIcon />
			</ActionButton>
			<ActionButton label={canRetry ? "Edit" : "Attach the files again to edit"} testid="user-edit" disabled={busy || !canRetry} onclick={startEditing}>
				<PencilIcon />
			</ActionButton>
			{#if turn.input}
				<CopyButton text={turn.input} testid="user-copy" />
			{/if}
		</div>
	{/if}
</div>
