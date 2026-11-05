<script lang="ts">
	import * as InputGroup from "$lib/components/ui/input-group";
	import { Badge } from "$lib/components/ui/badge";
	import { fileName } from "$lib/chat/store.svelte";
	import ArrowUpIcon from "@lucide/svelte/icons/arrow-up";
	import PaperclipIcon from "@lucide/svelte/icons/paperclip";
	import XIcon from "@lucide/svelte/icons/x";
	import SquareIcon from "@lucide/svelte/icons/square";

	let {
		busy,
		disabled = false,
		attachments = [],
		onSend,
		onStop,
		onAttach,
		onDetach,
		draft = null,
		onDraftTaken,
	}: {
		busy: boolean;
		disabled?: boolean;
		attachments?: string[];
		onSend: (text: string) => void;
		onStop: () => void;
		onAttach: () => void;
		onDetach: (path: string) => void;
		draft?: string | null;
		onDraftTaken?: () => void;
	} = $props();

	let text = $state("");
	const ready = $derived((text.trim().length > 0 || attachments.length > 0) && !busy && !disabled);

	$effect(() => {
		if (draft) {
			text = text ? `${text}\n${draft}` : draft;
			onDraftTaken?.();
		}
	});

	function submit() {
		if (!ready) return;
		onSend(text.trim());
		text = "";
	}
</script>

<form
	onsubmit={(event) => {
		event.preventDefault();
		submit();
	}}
>
	<InputGroup.Root>
		{#if attachments.length}
			<InputGroup.Addon align="block-start" class="flex-wrap" data-testid="pending-attachments">
				{#each attachments as path (path)}
					<Badge variant="secondary" class="max-w-64">
						<span class="truncate">{fileName(path)}</span>
						<button type="button" aria-label={`Remove ${fileName(path)}`} onclick={() => onDetach(path)}>
							<XIcon class="size-3" />
						</button>
					</Badge>
				{/each}
			</InputGroup.Addon>
		{/if}
		<InputGroup.Textarea
			bind:value={text}
			{disabled}
			rows={2}
			class="max-h-48"
			placeholder={disabled ? "Waiting for the agent to start" : "Message Steve"}
			aria-label="Message"
			data-testid="composer"
			onkeydown={(event: KeyboardEvent) => {
				if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
					event.preventDefault();
					submit();
				}
			}}
		/>
		<InputGroup.Addon align="block-end" class="justify-between">
			<InputGroup.Button
				type="button"
				variant="ghost"
				size="icon-sm"
				aria-label="Attach files"
				data-testid="attach"
				{disabled}
				onclick={onAttach}
			>
				<PaperclipIcon />
			</InputGroup.Button>
			{#if busy}
				<InputGroup.Button
					type="button"
					variant="secondary"
					size="icon-sm"
					aria-label="Stop"
					data-testid="stop"
					onclick={onStop}
				>
					<SquareIcon />
				</InputGroup.Button>
			{:else}
				<InputGroup.Button
					type="submit"
					variant="default"
					size="icon-sm"
					aria-label="Send"
					data-testid="send"
					disabled={!ready}
				>
					<ArrowUpIcon />
				</InputGroup.Button>
			{/if}
		</InputGroup.Addon>
	</InputGroup.Root>
</form>
