<script lang="ts">
	import * as Dialog from "$lib/components/ui/dialog";
	import * as DropdownMenu from "$lib/components/ui/dropdown-menu";
	import { Button, buttonVariants } from "$lib/components/ui/button";
	import { Spinner } from "$lib/components/ui/spinner";
	import CheckIcon from "@lucide/svelte/icons/check";
	import ChevronDownIcon from "@lucide/svelte/icons/chevron-down";
	import FolderIcon from "@lucide/svelte/icons/folder";
	import FolderOpenIcon from "@lucide/svelte/icons/folder-open";
	import { fileName, type ChatStore } from "$lib/chat/store.svelte";

	let { store }: { store: ChatStore } = $props();

	let error = $state<string | null>(null);
	let saving = $state(false);
	// A choice that would end open conversations, waiting to be confirmed.
	let confirming = $state<{ path: string | null } | null>(null);

	const current = $derived(store.workspace.path);
	const label = (path: string | null) => (path ? fileName(path) : "No folder");

	function choose(path: string | null) {
		error = null;
		if (path === current) return;
		if (store.replying) confirming = { path };
		else void save(path);
	}

	async function save(path: string | null) {
		saving = true;
		error = null;
		try {
			await store.setWorkspace(path);
		} catch (problem) {
			error = String(problem);
		} finally {
			saving = false;
		}
	}

	export async function other() {
		const chosen = await store.pickFolder();
		if (chosen) choose(chosen);
	}
</script>

<div class="flex min-w-0 items-center gap-2">
	<DropdownMenu.Root>
		<DropdownMenu.Trigger
			class={buttonVariants({ variant: "ghost", size: "sm", class: "text-muted-foreground max-w-64" })}
			title={current ?? "No working folder"}
			disabled={saving}
			data-testid="workspace-button"
		>
			{#if saving}
				<Spinner data-icon="inline-start" />
			{:else}
				<FolderIcon data-icon="inline-start" />
			{/if}
			<span class="truncate" data-testid="workspace-name">{label(current)}</span>
			<ChevronDownIcon data-icon="inline-end" />
		</DropdownMenu.Trigger>
		<DropdownMenu.Content side="top" align="start" class="w-80" data-testid="workspace-menu">
			{#each store.workspace.recent as path (path)}
				<DropdownMenu.Item title={path} data-testid="workspace-recent" onSelect={() => choose(path)}>
					<FolderIcon />
					<span class="flex min-w-0 flex-1 flex-col">
						<span class="truncate">{fileName(path)}</span>
						<span class="text-muted-foreground truncate text-xs">{path}</span>
					</span>
					{#if path === current}
						<CheckIcon aria-label="Current folder" />
					{/if}
				</DropdownMenu.Item>
			{/each}
			{#if store.workspace.recent.length}
				<DropdownMenu.Separator />
			{/if}
			<DropdownMenu.Item data-testid="workspace-other" onSelect={other}>
				<FolderOpenIcon />
				Other…
			</DropdownMenu.Item>
			{#if current}
				<DropdownMenu.Item data-testid="workspace-none" onSelect={() => choose(null)}>
					<FolderIcon />
					No folder
				</DropdownMenu.Item>
			{/if}
		</DropdownMenu.Content>
	</DropdownMenu.Root>
	{#if error}
		<p class="text-destructive truncate text-xs" title={error} data-testid="workspace-error">{error}</p>
	{/if}
</div>

<Dialog.Root
	open={confirming !== null}
	onOpenChange={(value) => {
		if (!value) confirming = null;
	}}
>
	<Dialog.Content data-testid="workspace-confirm-dialog">
		<Dialog.Header>
			<Dialog.Title>Switch to {label(confirming?.path ?? null)}?</Dialog.Title>
			<Dialog.Description>
				Changing the folder restarts Steve, which stops the reply in progress. Your conversations are saved, and stay with their folder.
			</Dialog.Description>
		</Dialog.Header>
		<Dialog.Footer>
			<Button variant="ghost" onclick={() => (confirming = null)}>Cancel</Button>
			<Button
				data-testid="workspace-confirm"
				onclick={() => {
					const path = confirming?.path ?? null;
					confirming = null;
					void save(path);
				}}
			>
				Switch folder
			</Button>
		</Dialog.Footer>
	</Dialog.Content>
</Dialog.Root>
