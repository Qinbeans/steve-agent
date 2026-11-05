<script lang="ts">
	import * as Dialog from "$lib/components/ui/dialog";
	import * as Field from "$lib/components/ui/field";
	import { Button, buttonVariants } from "$lib/components/ui/button";
	import { Checkbox } from "$lib/components/ui/checkbox";
	import { Input } from "$lib/components/ui/input";
	import { Spinner } from "$lib/components/ui/spinner";
	import { Textarea } from "$lib/components/ui/textarea";
	import SettingsIcon from "@lucide/svelte/icons/settings";
	import type { AgentSettings } from "$lib/bridge";
	import { parseHosts } from "$lib/chat/addons";
	import type { ChatStore } from "$lib/chat/store.svelte";

	let { store }: { store: ChatStore } = $props();

	let visible = $state(false);
	let settings = $state<AgentSettings | null>(null);
	let hosts = $state("");
	let timeout = $state("120");
	let saving = $state(false);
	let error = $state<string | null>(null);
	/** null while checking, then true, or the reason it cannot start. */
	let browser = $state<true | string | null>(null);
	let installing = $state(false);

	async function checkBrowser() {
		browser = null;
		try {
			await store.browserStatus();
			browser = true;
		} catch (problem) {
			browser = String(problem);
		}
	}

	export async function open() {
		error = null;
		settings = null;
		visible = true;
		try {
			const loaded = await store.getAgentSettings();
			settings = loaded;
			hosts = loaded.allowedHosts.join("\n");
			timeout = String(loaded.commandsTimeout);
		} catch (problem) {
			error = String(problem);
			return;
		}
		void checkBrowser();
	}

	async function install() {
		installing = true;
		try {
			await store.setupBrowser();
			await checkBrowser();
		} catch (problem) {
			browser = String(problem);
		} finally {
			installing = false;
		}
	}

	async function save() {
		if (!settings) return;
		saving = true;
		error = null;
		try {
			await store.setAgentSettings({
				...$state.snapshot(settings),
				allowedHosts: parseHosts(hosts),
				commandsTimeout: Number(timeout),
			});
			visible = false;
		} catch (problem) {
			error = String(problem);
		} finally {
			saving = false;
		}
	}
</script>

<button
	type="button"
	class={buttonVariants({ variant: "ghost", size: "icon-sm", class: "text-muted-foreground" })}
	aria-label="Settings"
	data-testid="settings-button"
	onclick={open}
>
	<SettingsIcon />
</button>

<Dialog.Root bind:open={visible}>
	<Dialog.Content class="max-h-[85vh] overflow-y-auto" data-testid="settings-dialog">
		<Dialog.Header>
			<Dialog.Title>Settings</Dialog.Title>
			<Dialog.Description>
				Saving restarts Steve{store.replying ? ", which stops the reply in progress" : ""}.
			</Dialog.Description>
		</Dialog.Header>
		{#if settings}
			<Field.Group>
				<Field.Set>
					<Field.Legend>Commands</Field.Legend>
					<Field.Description>
						In the working folder, in a bubblewrap sandbox. Not available without bubblewrap, or when the folder holds your home folder.
					</Field.Description>
					<Field.Field orientation="horizontal">
						<Checkbox id="commands-enabled" bind:checked={settings.commandsEnabled} data-testid="setting-commands" />
						<Field.Label for="commands-enabled">Let the agent run commands</Field.Label>
					</Field.Field>
					<Field.Field orientation="horizontal">
						<Checkbox
							id="commands-network"
							bind:checked={settings.commandsNetwork}
							disabled={!settings.commandsEnabled}
							data-testid="setting-network"
						/>
						<Field.Content>
							<Field.Label for="commands-network">Commands can use the network</Field.Label>
							<Field.Description>
								Needed for package installs. Commands can then also reach this machine's own services and your local network.
							</Field.Description>
						</Field.Content>
					</Field.Field>
					<Field.Field>
						<Field.Label for="commands-timeout">Time limit, in seconds</Field.Label>
						<Input
							id="commands-timeout"
							type="number"
							min="1"
							bind:value={timeout}
							disabled={!settings.commandsEnabled}
							data-testid="setting-timeout"
						/>
					</Field.Field>
				</Field.Set>
				<Field.Set>
					<Field.Legend>Web</Field.Legend>
					<Field.Field orientation="horizontal">
						<Checkbox id="web-enabled" bind:checked={settings.webEnabled} data-testid="setting-web" />
						<Field.Label for="web-enabled">Let the agent read the web</Field.Label>
					</Field.Field>
					<Field.Field orientation="horizontal">
						<Checkbox id="obey-robots" bind:checked={settings.obeyRobots} disabled={!settings.webEnabled} data-testid="setting-robots" />
						<Field.Content>
							<Field.Label for="obey-robots">Obey robots.txt</Field.Label>
							<Field.Description>Sites use it to say what bots may read.</Field.Description>
						</Field.Content>
					</Field.Field>
					<Field.Field>
						<Field.Label for="allowed-hosts">Private addresses it may reach</Field.Label>
						<Textarea
							id="allowed-hosts"
							rows={3}
							bind:value={hosts}
							disabled={!settings.webEnabled}
							placeholder={"nas.local\n*.home.arpa\n192.168.1.0/24"}
							data-testid="setting-hosts"
						/>
						<Field.Description>This machine and your local network are blocked unless listed here. One per line.</Field.Description>
					</Field.Field>
					<Field.Field>
						<Field.Label for="browser">Browser</Field.Label>
						<Input
							id="browser"
							bind:value={settings.browser}
							disabled={!settings.webEnabled}
							placeholder="Steve's own"
							data-testid="setting-browser"
						/>
						<Field.Description>
							chrome, msedge, or the path of a Chromium-based browser. Your profile and logins are not used. Save before checking a new one.
						</Field.Description>
						<div class="flex min-w-0 items-center gap-2 text-xs" data-testid="browser-status">
							{#if browser === null}
								<Spinner /> Checking…
							{:else if browser === true}
								<span class="text-muted-foreground">The browser works.</span>
							{:else}
								<span class="text-destructive min-w-0 truncate" title={browser}>The browser could not start.</span>
								{#if !settings.browser.trim()}
									<Button variant="secondary" size="sm" disabled={installing} data-testid="browser-install" onclick={install}>
										{#if installing}<Spinner data-icon="inline-start" />{/if}
										Download it (about 120 MB)
									</Button>
								{/if}
							{/if}
						</div>
					</Field.Field>
					<Field.Field>
						<Field.Label for="search-url">Search page</Field.Label>
						<Input
							id="search-url"
							bind:value={settings.searchUrl}
							disabled={!settings.webEnabled}
							placeholder="https://example.com/search?q={'{query}'}"
							data-testid="setting-search"
						/>
						<Field.Description>
							Optional, and needs no token. Most search pages refuse bots or disallow them in robots.txt, so it may only work with that turned off.
						</Field.Description>
					</Field.Field>
				</Field.Set>
			</Field.Group>
		{:else if !error}
			<div class="flex justify-center p-6"><Spinner /></div>
		{/if}
		{#if error}
			<p class="text-destructive text-sm" data-testid="settings-error">{error}</p>
		{/if}
		<Dialog.Footer>
			<Button variant="ghost" onclick={() => (visible = false)}>Cancel</Button>
			<Button disabled={!settings || saving} data-testid="settings-save" onclick={save}>
				{#if saving}<Spinner data-icon="inline-start" />{/if}
				Save
			</Button>
		</Dialog.Footer>
	</Dialog.Content>
</Dialog.Root>
