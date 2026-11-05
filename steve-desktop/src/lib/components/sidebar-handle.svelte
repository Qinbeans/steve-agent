<script lang="ts">
	import { useSidebar } from "$lib/components/ui/sidebar";
	import { DEFAULT_SIDEBAR_WIDTH, clampSidebarWidth, saveSidebarWidth } from "$lib/sidebar-width";

	const KEY_STEP = 16;

	let { width = $bindable(), dragging = $bindable() }: { width: number; dragging: boolean } = $props();

	const sidebar = useSidebar();

	/** For the menu, which sits outside the sidebar's context. */
	export function toggle() {
		sidebar.toggle();
	}

	function set(value: number) {
		width = clampSidebarWidth(value, window.innerWidth);
		saveSidebarWidth(width);
	}

	function move(event: PointerEvent) {
		if (dragging) set(event.clientX);
	}
</script>

<svelte:window onresize={() => (width = clampSidebarWidth(width, window.innerWidth))} />

{#if !sidebar.isMobile && sidebar.state === "expanded"}
	<!-- A focusable separator is an interactive control in ARIA, which these checks do not know. -->
	<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
	<div
		role="separator"
		aria-orientation="vertical"
		aria-label="Resize sidebar"
		aria-valuenow={width}
		aria-valuemin={180}
		aria-valuemax={480}
		tabindex="0"
		title="Drag to resize. Double-click to reset."
		class="hover:bg-border focus-visible:bg-ring active:bg-ring absolute inset-y-0 right-0 z-20 w-1.5 cursor-col-resize outline-none transition-colors"
		data-testid="sidebar-handle"
		onpointerdown={(event) => {
			event.preventDefault();
			event.currentTarget.setPointerCapture(event.pointerId);
			dragging = true;
		}}
		onpointermove={move}
		onpointerup={() => (dragging = false)}
		onpointercancel={() => (dragging = false)}
		ondblclick={() => set(DEFAULT_SIDEBAR_WIDTH)}
		onkeydown={(event) => {
			if (event.key === "ArrowLeft") set(width - KEY_STEP);
			else if (event.key === "ArrowRight") set(width + KEY_STEP);
			else return;
			event.preventDefault();
		}}
	></div>
{/if}
