import { marked } from "marked";

/** The words of some Markdown, without its markup. DOMParser runs no scripts and loads nothing. */
export function plainText(markdown: string): string {
	const html = marked.parse(markdown, { async: false, gfm: true });
	const text = new DOMParser().parseFromString(html, "text/html").body.textContent ?? "";
	return text.replace(/\s+/g, " ").trim();
}

/** Reads one message aloud at a time, with the system's voices. */
class Speech {
	speakingId = $state<string | null>(null);

	get available() {
		return typeof speechSynthesis !== "undefined";
	}

	toggle(id: string, markdown: string) {
		const wasSpeaking = this.speakingId === id;
		speechSynthesis.cancel();
		this.speakingId = null;
		if (wasSpeaking) return;
		const utterance = new SpeechSynthesisUtterance(plainText(markdown));
		// Only the latest message's end counts: cancelling one ends it too.
		const finished = () => {
			if (this.speakingId === id) this.speakingId = null;
		};
		utterance.onend = finished;
		utterance.onerror = finished;
		this.speakingId = id;
		speechSynthesis.speak(utterance);
	}
}

export const speech = new Speech();
