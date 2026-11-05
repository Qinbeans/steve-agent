<script lang="ts">
	import { setContext, tick } from "svelte";
	import { mode, toggleMode } from "mode-watcher";
	import * as Alert from "$lib/components/ui/alert";
	import * as Dialog from "$lib/components/ui/dialog";
	import * as Empty from "$lib/components/ui/empty";
	import * as Sidebar from "$lib/components/ui/sidebar";
	import { Button } from "$lib/components/ui/button";
	import { Spinner } from "$lib/components/ui/spinner";
	import CircleAlertIcon from "@lucide/svelte/icons/circle-alert";
	import MessageSquareIcon from "@lucide/svelte/icons/message-square";
	import MoonIcon from "@lucide/svelte/icons/moon";
	import PlusIcon from "@lucide/svelte/icons/plus";
	import SunIcon from "@lucide/svelte/icons/sun";
	import Trash2Icon from "@lucide/svelte/icons/trash-2";
	import Composer from "$lib/components/composer.svelte";
	import ContextPie from "$lib/components/context-pie.svelte";
	import FolderSelect from "$lib/components/folder-select.svelte";
	import AgentActions from "$lib/components/agent-actions.svelte";
	import UserMessage from "$lib/components/user-message.svelte";
	import McpMenu from "$lib/components/mcp-menu.svelte";
	import ModeSelect from "$lib/components/mode-select.svelte";
	import ApprovalCard from "$lib/components/approval-card.svelte";
	import ElicitationForm from "$lib/components/elicitation-form.svelte";
	import SkillsMenu from "$lib/components/skills-menu.svelte";
	import SettingsDialog from "$lib/components/settings-dialog.svelte";
	import SidebarHandle from "$lib/components/sidebar-handle.svelte";
	import { DEFAULT_SIDEBAR_WIDTH, loadSidebarWidth } from "$lib/sidebar-width";
	import { hideSplash, setSplashStatus, splashMessage } from "$lib/splash";
	import SubagentSheet from "$lib/components/subagent-sheet.svelte";
	import TurnOutput from "$lib/components/turn-output.svelte";
	import { loadBridge, type Bridge } from "$lib/bridge";
	import { ChatStore, type Conversation } from "$lib/chat/store.svelte";
	import { canRetry } from "$lib/chat/turn";
	import type { Item } from "$lib/chat/turn";

	type Tool = Extract<Item, { kind: "tool" }>;

	let store = $state<ChatStore | null>(null);
	setContext("chat-store", () => store);
	let bridge: Bridge | null = null;
	let watching = $state<Tool | null>(null);
	let scroller = $state<HTMLElement | null>(null);
	let pinned = true;
	let sidebarWidth = $state(typeof window === "undefined" ? DEFAULT_SIDEBAR_WIDTH : loadSidebarWidth(window.innerWidth));
	let resizing = $state(false);
	let handle = $state<SidebarHandle | null>(null);
	let folderSelect = $state<FolderSelect | null>(null);
	let mcpMenu = $state<McpMenu | null>(null);
	let skillsMenu = $state<SkillsMenu | null>(null);
	let settingsDialog = $state<SettingsDialog | null>(null);

	function onMenu(id: string) {
		switch (id) {
			case "new-chat":
				return store?.newConversation();
			case "open-folder":
				return void folderSelect?.other();
			case "attach":
				return void store?.pickFiles();
			case "toggle-sidebar":
				return handle?.toggle();
			case "toggle-theme":
				return toggleMode();
			case "compact":
				return void (store?.active && store.compact(store.active));
			case "settings":
				return void settingsDialog?.open();
			case "add-mcp":
				return mcpMenu?.openAdd();
			case "add-skill":
				return void skillsMenu?.add();
		}
	}

	$effect(() => {
		void loadBridge().then((loaded) => {
			bridge = loaded;
			store = new ChatStore(loaded);
			void store.load();
			loaded.onMenu(onMenu);
		});
	});

	// Only the first start: after that, a restart shows in the conversation area instead.
	let splashDone = false;
	$effect(() => {
		if (!store || splashDone) return;
		if (store.status.state !== "starting") {
			splashDone = true;
			hideSplash();
		} else if (store.addonsLoaded) {
			setSplashStatus(splashMessage(store.mcpServers.length));
		}
	});

	const openLink = (url: string) => void bridge?.openExternal(url);

	// Follow new output, unless the reader has scrolled up to read something.
	function onScroll() {
		if (scroller) pinned = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
	}

	$effect(() => {
		JSON.stringify(store?.active?.turns);
		void tick().then(() => {
			if (pinned && scroller) scroller.scrollTop = scroller.scrollHeight;
		});
	});

	// Retrying or editing drops what came after, so that needs a yes if there is anything to lose.
	let retrying = $state<{ conversation: Conversation; index: number; input?: string; later: number } | null>(null);

	function askToRetry(conversation: Conversation, index: number, input?: string) {
		const later = conversation.turns.length - index - 1;
		if (later > 0) retrying = { conversation, index, input, later };
		else runRetry(conversation, index, input);
	}

	function runRetry(conversation: Conversation, index: number, input?: string) {
		pinned = true;
		void store?.retry(conversation, index, input);
	}

	function send(text: string) {
		pinned = true;
		void store?.send(text);
	}
</script>

<Sidebar.Provider
	class={[
		"h-svh",
		resizing && "cursor-col-resize select-none [&_[data-slot=sidebar-container]]:transition-none [&_[data-slot=sidebar-gap]]:transition-none",
	]
		.filter(Boolean)
		.join(" ")}
	style={`--sidebar-width: ${sidebarWidth}px;`}
>
	<Sidebar.Root>
		<SidebarHandle bind:this={handle} bind:width={sidebarWidth} bind:dragging={resizing} />
		<Sidebar.Header>
			<Button variant="outline" class="w-full justify-start" data-testid="new-chat" onclick={() => store?.newConversation()}>
				<PlusIcon data-icon="inline-start" />
				New conversation
			</Button>
		</Sidebar.Header>
		<Sidebar.Content>
			<Sidebar.Group>
				<Sidebar.GroupContent>
					<Sidebar.Menu class="gap-1">
						{#each store?.conversations ?? [] as conversation (conversation.id)}
							<Sidebar.MenuItem data-testid="conversation">
								<Sidebar.MenuButton
									class="border-foreground/15 hover:border-foreground/35 focus-visible:bg-sidebar-accent rounded-lg border bg-transparent hover:bg-transparent"
									isActive={conversation.id === store?.activeId}
									onclick={() => void store?.select(conversation.id)}
								>
									{#if conversation.streamId}
										<Spinner />
									{:else}
										<MessageSquareIcon />
									{/if}
									<span>{conversation.title}</span>
								</Sidebar.MenuButton>
								<Sidebar.MenuAction
									showOnHover
									aria-label="Delete conversation"
									onclick={() => store?.remove(conversation.id)}
								>
									<Trash2Icon />
								</Sidebar.MenuAction>
							</Sidebar.MenuItem>
						{/each}
					</Sidebar.Menu>
				</Sidebar.GroupContent>
			</Sidebar.Group>
		</Sidebar.Content>
	</Sidebar.Root>

	<Sidebar.Inset class="min-h-0 min-w-0">
		<header class="glass flex h-12 shrink-0 items-center gap-2 border-0 border-b px-3">
			<Sidebar.Trigger />
			<h1 class="min-w-0 flex-1 truncate text-sm font-medium">{store?.active?.title ?? "Steve"}</h1>
			{#if store?.context && store.active}
				<ContextPie
					context={store.context}
					busy={Boolean(store.active.streamId)}
					onCompact={() => store?.active && store.compact(store.active)}
				/>
			{/if}
			<Button variant="ghost" size="icon-sm" aria-label="Toggle theme" onclick={toggleMode}>
				{#if mode.current === "dark"}
					<SunIcon />
				{:else}
					<MoonIcon />
				{/if}
			</Button>
		</header>

		<div bind:this={scroller} onscroll={onScroll} class="min-h-0 flex-1 overflow-y-auto">
			<div class="mx-auto flex w-full max-w-3xl flex-col gap-6 p-4">
				{#if store?.status.state === "failed"}
					<Alert.Root variant="destructive" data-testid="backend-error">
						<CircleAlertIcon />
						<Alert.Title>The agent is not running</Alert.Title>
						<Alert.Description class="flex flex-col items-start gap-2">
							<span class="break-words">{store.status.message}</span>
							<Button variant="outline" size="sm" data-testid="retry-backend" onclick={() => store?.start()}>
								Try again
							</Button>
						</Alert.Description>
					</Alert.Root>
				{/if}

				{#if store?.active}
					{#each store.active.turns as turn, index (index)}
						{@const conversation = store.active}
						{@const busy = Boolean(conversation.streamId)}
						{@const actionsClass = index === conversation.turns.length - 1 ? "" : "opacity-0 transition-opacity group-hover/turn:opacity-100 focus-within:opacity-100"}
						<div class="group/turn flex flex-col gap-3" data-testid="turn">
							{#if turn.input || turn.attachments.length}
								<UserMessage
									{turn}
									{busy}
									{actionsClass}
									onLink={openLink}
									canRetry={canRetry(turn)}
									onRetry={() => askToRetry(conversation, index)}
									onEdit={(text) => askToRetry(conversation, index, text)}
								/>
							{/if}
							<TurnOutput {turn} onLink={openLink} onWatch={(tool) => (watching = tool)} />
							{#each turn.questions as question (question.id)}
								{#if question.kind === "approval"}
									<ApprovalCard {question} onAnswer={(answer) => void store?.answerQuestion(turn, question, answer)} />
								{:else}
									<ElicitationForm {question} onLink={openLink} onAnswer={(answer) => void store?.answerQuestion(turn, question, answer)} />
								{/if}
							{/each}
							{#if turn.status !== "streaming" && !turn.compaction}
								<div class={actionsClass}>
									<AgentActions
										{turn}
										{busy}
										canRetry={canRetry(turn)}
										onRate={(rating) => void store?.rate(conversation, turn, rating)}
										onRetry={() => askToRetry(conversation, index)}
									/>
								</div>
							{/if}
						</div>
					{/each}
				{:else if store?.status.state === "starting"}
					<Empty.Root class="pt-24" data-testid="backend-starting">
						<Empty.Header>
							<Empty.Media variant="icon"><Spinner /></Empty.Media>
							<Empty.Title>Starting the agent</Empty.Title>
							<Empty.Description>Running steve-agent serve in the background.</Empty.Description>
						</Empty.Header>
					</Empty.Root>
				{:else if store?.status.state === "ready"}
					<Empty.Root class="pt-24" data-testid="empty-chat">
						<Empty.Header>
							<Empty.Media variant="icon"><MessageSquareIcon /></Empty.Media>
							<Empty.Title>What are we researching?</Empty.Title>
							<Empty.Description>Tool calls and subagents show up here as Steve works.</Empty.Description>
						</Empty.Header>
					</Empty.Root>
				{/if}
			</div>
		</div>

		<div class="shrink-0 p-4 pt-0">
			<div class="mx-auto w-full max-w-3xl">
				<Composer
					busy={Boolean(store?.active?.streamId)}
					disabled={store?.status.state !== "ready"}
					attachments={store?.pending ?? []}
					draft={store?.draft}
					onDraftTaken={() => {
						if (store) store.draft = null;
					}}
					onSend={send}
					onAttach={() => store?.pickFiles()}
					onDetach={(path) => store?.detach(path)}
					onStop={() => store?.active && store.stop(store.active)}
				/>
				{#if store}
					<div class="mt-1 flex flex-wrap items-center gap-1">
						<ModeSelect {store} />
						<FolderSelect bind:this={folderSelect} {store} />
						<McpMenu bind:this={mcpMenu} {store} />
						<SkillsMenu bind:this={skillsMenu} {store} />
						<SettingsDialog bind:this={settingsDialog} {store} />
					</div>
				{/if}
			</div>
		</div>
	</Sidebar.Inset>
</Sidebar.Provider>

<Dialog.Root
	open={retrying !== null}
	onOpenChange={(value) => {
		if (!value) retrying = null;
	}}
>
	<Dialog.Content data-testid="retry-dialog">
		<Dialog.Header>
			<Dialog.Title>{retrying?.input === undefined ? "Retry from here?" : "Send your edit?"}</Dialog.Title>
			<Dialog.Description>
				This removes {retrying?.later === 1 ? "the 1 later exchange" : `the ${retrying?.later} later exchanges`} in this conversation.
			</Dialog.Description>
		</Dialog.Header>
		<Dialog.Footer>
			<Button variant="ghost" onclick={() => (retrying = null)}>Cancel</Button>
			<Button
				data-testid="retry-confirm"
				onclick={() => {
					if (retrying) runRetry(retrying.conversation, retrying.index, retrying.input);
					retrying = null;
				}}
			>
				Continue
			</Button>
		</Dialog.Footer>
	</Dialog.Content>
</Dialog.Root>

{#if store}
	<SubagentSheet {store} bind:tool={watching} onLink={openLink} />
{/if}
