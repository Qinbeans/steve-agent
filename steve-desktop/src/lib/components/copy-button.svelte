<script lang="ts">
	import { getContext } from "svelte";
	import CheckIcon from "@lucide/svelte/icons/check";
	import CircleAlertIcon from "@lucide/svelte/icons/circle-alert";
	import CopyIcon from "@lucide/svelte/icons/copy";
	import ActionButton from "$lib/components/action-button.svelte";
	import type { ChatStore } from "$lib/chat/store.svelte";

	let { text, testid }: { text: string; testid: string } = $props();

	const getStore = getContext<() => ChatStore | null>("chat-store");
	const RESET_MS = 1500;
	let result = $state<"copied" | "failed" | null>(null);
	let timer: ReturnType<typeof setTimeout>;

	async function copy() {
		try {
			await getStore()?.copyText(text);
			result = "copied";
		} catch {
			result = "failed";
		}
		clearTimeout(timer);
		timer = setTimeout(() => (result = null), RESET_MS);
	}
</script>

<ActionButton label={result === "failed" ? "Could not copy" : result === "copied" ? "Copied" : "Copy"} {testid} onclick={copy}>
	{#if result === "copied"}
		<CheckIcon />
	{:else if result === "failed"}
		<CircleAlertIcon />
	{:else}
		<CopyIcon />
	{/if}
</ActionButton>
