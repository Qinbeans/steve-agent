<script lang="ts">
	import { Button } from "$lib/components/ui/button";
	import { Input } from "$lib/components/ui/input";
	import ShieldQuestionIcon from "@lucide/svelte/icons/shield-question";
	import { formatArgs, type Question } from "$lib/chat/turn";

	let { question, onAnswer }: { question: Question; onAnswer: (answer: unknown) => void } = $props();

	let reason = $state("");
	const args = $derived(JSON.stringify(question.arguments ?? {}));
</script>

<div class="glass flex flex-col gap-2 rounded-lg border p-3 text-sm" data-testid="approval-card">
	<p class="flex items-center gap-2 font-medium">
		<ShieldQuestionIcon class="size-4" />
		<span>Allow <span class="font-mono" data-testid="approval-tool">{question.tool}</span>?</span>
	</p>
	{#if args !== "{}"}
		<pre class="bg-muted max-h-64 overflow-auto rounded-sm p-2 text-xs wrap-anywhere whitespace-pre-wrap" data-testid="approval-arguments">{formatArgs(args)}</pre>
	{/if}
	<Input bind:value={reason} placeholder="Tell Steve why, if you deny (optional)" aria-label="Reason for denying" data-testid="approval-reason" />
	<div class="flex justify-end gap-2">
		<Button variant="ghost" size="sm" disabled={question.answered} data-testid="approval-deny" onclick={() => onAnswer({ approve: false, reason })}>Deny</Button>
		<Button size="sm" disabled={question.answered} data-testid="approval-allow" onclick={() => onAnswer({ approve: true })}>Allow</Button>
	</div>
</div>
