<script lang="ts">
	import type { Snippet } from "svelte";
	import { Button } from "$lib/components/ui/button";

	let {
		label,
		testid,
		pressed,
		disabled = false,
		onclick,
		children,
	}: {
		label: string;
		testid: string;
		pressed?: boolean;
		disabled?: boolean;
		onclick: () => void;
		children: Snippet;
	} = $props();
</script>

<Button
	variant="ghost"
	size="icon-sm"
	class="text-muted-foreground hover:text-foreground aria-pressed:text-foreground"
	title={label}
	aria-label={label}
	aria-pressed={pressed}
	{disabled}
	data-testid={testid}
	{onclick}
>
	{@render children()}
</Button>
