<script lang="ts">
	import DOMPurify from "dompurify";
	import { marked } from "marked";

	let {
		text,
		onLink,
		breaks = false,
	}: {
		text: string;
		onLink: (url: string) => void;
		/** Keeps single line breaks, for text someone typed rather than Markdown written to be rendered. */
		breaks?: boolean;
	} = $props();

	// Agent output can quote anything it read, including hostile pages, and a person can paste the
	// same into a message, so neither reaches the DOM unsanitized.
	const html = $derived(
		DOMPurify.sanitize(marked.parse(text, { async: false, gfm: true, breaks }), {
			FORBID_TAGS: ["style", "form", "input", "button", "iframe", "img"],
			FORBID_ATTR: ["style"],
		}),
	);

	function click(event: MouseEvent) {
		const anchor = (event.target as Element).closest("a");
		if (!anchor) return;
		// A link must not navigate the app's own window away.
		event.preventDefault();
		if (/^https?:/i.test(anchor.href)) onLink(anchor.href);
	}
</script>

<!-- eslint-disable-next-line svelte/no-at-html-tags -->
<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="prose prose-sm dark:prose-invert prose-code:before:content-none prose-code:after:content-none max-w-none wrap-anywhere" onclick={click}>{@html html}</div>
