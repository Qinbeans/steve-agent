<script lang="ts">
	import * as Dialog from "$lib/components/ui/dialog";
	import * as DropdownMenu from "$lib/components/ui/dropdown-menu";
	import { Badge } from "$lib/components/ui/badge";
	import { Button, buttonVariants } from "$lib/components/ui/button";
	import ChevronDownIcon from "@lucide/svelte/icons/chevron-down";
	import PlusIcon from "@lucide/svelte/icons/plus";
	import SparklesIcon from "@lucide/svelte/icons/sparkles";
	import type { Skill } from "$lib/bridge";
	import type { ChatStore } from "$lib/chat/store.svelte";

	let { store }: { store: ChatStore } = $props();

	let viewing = $state<Skill | null>(null);
	let error = $state<string | null>(null);

	export async function add() {
		error = null;
		try {
			await store.addSkillFromFolder();
		} catch (problem) {
			error = String(problem);
		}
	}

	async function remove(name: string) {
		error = null;
		try {
			await store.removeSkill(name);
			viewing = null;
		} catch (problem) {
			error = String(problem);
		}
	}
</script>

<DropdownMenu.Root>
	<DropdownMenu.Trigger
		class={buttonVariants({ variant: "ghost", size: "sm", class: "text-muted-foreground" })}
		data-testid="skills-button"
	>
		<SparklesIcon data-icon="inline-start" />
		Skills
		{#if store.skills.length}
			<span class="text-xs">{store.skills.length}</span>
		{/if}
		<ChevronDownIcon data-icon="inline-end" />
	</DropdownMenu.Trigger>
	<DropdownMenu.Content side="top" align="start" class="w-80" data-testid="skills-menu">
		{#each store.skills as skill (`${skill.scope}/${skill.name}`)}
			<DropdownMenu.Item
				data-testid="skill"
				onSelect={() => {
					error = null;
					viewing = skill;
				}}
			>
				<span class="flex min-w-0 flex-1 flex-col">
					<span class="truncate">{skill.name}</span>
					<span class="text-muted-foreground truncate text-xs">{skill.description}</span>
				</span>
				{#if skill.scope === "project"}
					<Badge variant="outline">Project</Badge>
				{/if}
			</DropdownMenu.Item>
		{/each}
		{#if store.skills.length}
			<DropdownMenu.Separator />
		{/if}
		<DropdownMenu.Item data-testid="skill-add" onSelect={add}>
			<PlusIcon />
			Add skill from a folder…
		</DropdownMenu.Item>
	</DropdownMenu.Content>
</DropdownMenu.Root>
{#if error && !viewing}
	<p class="text-destructive truncate text-xs" title={error} data-testid="skill-error">{error}</p>
{/if}

<Dialog.Root
	open={viewing !== null}
	onOpenChange={(value) => {
		if (!value) viewing = null;
	}}
>
	<Dialog.Content data-testid="skill-details">
		{#if viewing}
			<Dialog.Header>
				<Dialog.Title>{viewing.name}</Dialog.Title>
				<Dialog.Description>{viewing.description}</Dialog.Description>
			</Dialog.Header>
			{#if viewing.scope === "project"}
				<p class="text-muted-foreground text-xs">This skill comes from the working folder's .agents/skills.</p>
			{:else}
				<p class="text-muted-foreground text-xs">Removing deletes Steve's copy of this skill.</p>
			{/if}
			{#if error}
				<p class="text-destructive text-xs">{error}</p>
			{/if}
			{#if viewing.scope === "personal"}
				<Dialog.Footer>
					<Button
						variant="ghost"
						data-testid="skill-remove"
						onclick={() => {
							if (viewing) void remove(viewing.name);
						}}
					>
						Remove
					</Button>
				</Dialog.Footer>
			{/if}
		{/if}
	</Dialog.Content>
</Dialog.Root>
