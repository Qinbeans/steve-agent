<script lang="ts">
	import { Button } from "$lib/components/ui/button";
	import { Checkbox } from "$lib/components/ui/checkbox";
	import { Input } from "$lib/components/ui/input";
	import { Label } from "$lib/components/ui/label";
	import ExternalLinkIcon from "@lucide/svelte/icons/external-link";
	import MessageCircleQuestionIcon from "@lucide/svelte/icons/message-circle-question";
	import Markdown from "$lib/components/markdown.svelte";
	import { collect, initialValues, parseFields, type Field, type Value } from "$lib/chat/elicitation";
	import type { Question } from "$lib/chat/turn";

	let {
		question,
		onAnswer,
		onLink,
	}: { question: Question; onAnswer: (answer: unknown) => void; onLink: (url: string) => void } = $props();

	const fields = $derived(parseFields(question.schema));
	const isUrl = $derived(question.mode === "url");
	// svelte-ignore state_referenced_locally
	let values = $state<Record<string, Value>>(initialValues(parseFields(question.schema)));
	let errors = $state<Record<string, string>>({});
	/** Text typed for a choice that allows it, which is the answer while it has any. */
	let custom = $state<Record<string, string>>({});

	function submit() {
		const entered = { ...values };
		for (const [name, text] of Object.entries(custom)) if (text.trim()) entered[name] = text;
		const result = collect(fields, entered);
		errors = result.errors;
		if (Object.keys(result.errors).length === 0) onAnswer({ action: "accept", content: result.content });
	}

	function toggle(field: Field, value: string) {
		const picked = values[field.name] as string[];
		values[field.name] = picked.includes(value) ? picked.filter((other) => other !== value) : [...picked, value];
	}
</script>

<form
	class="glass flex flex-col gap-3 rounded-lg border p-3 text-sm"
	data-testid="elicitation-form"
	onsubmit={(event) => {
		event.preventDefault();
		if (isUrl) onAnswer({ action: "accept" });
		else submit();
	}}
>
	<div class="flex items-start gap-2">
		<MessageCircleQuestionIcon class="mt-0.5 size-4 shrink-0" />
		<div class="min-w-0 flex-1" data-testid="elicitation-message">
			<p class="text-muted-foreground text-xs">{question.server} asks:</p>
			<Markdown text={question.message ?? ""} {onLink} breaks />
		</div>
	</div>

	{#if isUrl}
		<div class="flex flex-col items-start gap-2">
			<p class="bg-muted w-full rounded-sm p-2 font-mono text-xs wrap-anywhere" data-testid="elicitation-url">{question.url}</p>
			<Button type="button" variant="outline" size="sm" data-testid="elicitation-open" onclick={() => question.url && onLink(question.url)}>
				<ExternalLinkIcon data-icon="inline-start" />
				Open in browser
			</Button>
		</div>
	{/if}

	{#each fields as field (field.name)}
		<div class="flex flex-col gap-1.5" data-testid="elicitation-field" data-field={field.name}>
			{#if field.kind !== "boolean" && field.label}
				<Label for="elicit-{field.name}">{field.label}{field.required ? "" : " (optional)"}</Label>
			{/if}
			{#if field.description}
				<p class="text-muted-foreground text-xs">{field.description}</p>
			{/if}

			{#if field.kind === "choice" || field.kind === "multi"}
				<div class="flex flex-wrap gap-1.5" role={field.kind === "choice" ? "radiogroup" : "group"} aria-label={field.label || question.message}>
					{#each field.options as option (option.value)}
						{@const chosen = field.kind === "multi" ? (values[field.name] as string[]).includes(option.value) : values[field.name] === option.value && !custom[field.name]?.trim()}
						<Button
							type="button"
							size="sm"
							variant={chosen ? "default" : "outline"}
							aria-pressed={chosen}
							data-testid="elicitation-choice"
							data-value={option.value}
							onclick={() => {
								if (field.kind === "multi") toggle(field, option.value);
								else {
									values[field.name] = option.value;
									custom[field.name] = "";
								}
							}}
						>
							{option.label}
						</Button>
					{/each}
				</div>
				{#if field.allowCustom}
					<Input
						bind:value={custom[field.name]}
						id="elicit-{field.name}"
						placeholder={field.options.length ? "Or type your own" : "Type your answer"}
						autocomplete="off"
						data-testid="elicitation-custom"
					/>
				{/if}
			{:else if field.kind === "boolean"}
				<div class="flex items-center gap-2">
					<Checkbox
						id="elicit-{field.name}"
						checked={values[field.name] === true}
						onCheckedChange={(checked) => (values[field.name] = checked === true)}
						data-testid="elicitation-check"
					/>
					<Label for="elicit-{field.name}">{field.label}</Label>
				</div>
			{:else}
				<Input
					id="elicit-{field.name}"
					type={field.kind === "text" ? field.inputType : "text"}
					inputmode={field.kind === "integer" ? "numeric" : field.kind === "number" ? "decimal" : undefined}
					value={values[field.name] as string}
					oninput={(event) => (values[field.name] = event.currentTarget.value)}
					aria-invalid={errors[field.name] ? true : undefined}
					autocomplete="off"
					data-testid="elicitation-input"
				/>
			{/if}
			{#if errors[field.name]}
				<p class="text-destructive text-xs" data-testid="elicitation-error">{errors[field.name]}</p>
			{/if}
		</div>
	{/each}

	<div class="flex justify-end gap-2">
		<Button type="button" variant="ghost" size="sm" disabled={question.answered} data-testid="elicitation-cancel" onclick={() => onAnswer({ action: "cancel" })}>Cancel</Button>
		<Button type="button" variant="ghost" size="sm" disabled={question.answered} data-testid="elicitation-decline" onclick={() => onAnswer({ action: "decline" })}>Decline</Button>
		<Button type="submit" size="sm" disabled={question.answered} data-testid="elicitation-submit">{isUrl ? "I've finished" : "Send"}</Button>
	</div>
</form>
