<script lang="ts">
	import * as DropdownMenu from "$lib/components/ui/dropdown-menu";
	import { buttonVariants } from "$lib/components/ui/button";
	import BrainIcon from "@lucide/svelte/icons/brain";
	import CheckIcon from "@lucide/svelte/icons/check";
	import ChevronDownIcon from "@lucide/svelte/icons/chevron-down";
	import HandIcon from "@lucide/svelte/icons/hand";
	import ZapIcon from "@lucide/svelte/icons/zap";
	import type { Mode } from "$lib/bridge";
	import type { ChatStore } from "$lib/chat/store.svelte";

	let { store }: { store: ChatStore } = $props();

	const MODES = [
		{ id: "manual", label: "Manual", about: "Asks before changing anything", icon: HandIcon },
		{ id: "auto", label: "Auto", about: "Changes things without asking", icon: ZapIcon },
		{ id: "planning", label: "Planning", about: "Only thinks and plans, and changes nothing", icon: BrainIcon },
	] as const satisfies readonly { id: Mode; label: string; about: string; icon: unknown }[];

	const current = $derived(MODES.find((mode) => mode.id === store.mode) ?? MODES[0]);
</script>

<DropdownMenu.Root>
	<DropdownMenu.Trigger
		class={buttonVariants({ variant: "ghost", size: "sm", class: "text-muted-foreground" })}
		title="{current.label}: {current.about}"
		data-testid="mode-button"
	>
		<current.icon data-icon="inline-start" />
		<span data-testid="mode-name">{current.label}</span>
		<ChevronDownIcon data-icon="inline-end" />
	</DropdownMenu.Trigger>
	<DropdownMenu.Content side="top" align="start" class="w-72" data-testid="mode-menu">
		{#each MODES as mode (mode.id)}
			<DropdownMenu.Item data-testid="mode-{mode.id}" onSelect={() => store.setMode(mode.id)}>
				<mode.icon />
				<span class="flex min-w-0 flex-1 flex-col">
					<span>{mode.label}</span>
					<span class="text-muted-foreground text-xs">{mode.about}</span>
				</span>
				{#if mode.id === store.mode}
					<CheckIcon aria-label="Current mode" />
				{/if}
			</DropdownMenu.Item>
		{/each}
	</DropdownMenu.Content>
</DropdownMenu.Root>
