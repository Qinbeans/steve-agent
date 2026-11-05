export const DEFAULT_SIDEBAR_WIDTH = 256;
const MIN_WIDTH = 180;
const MAX_WIDTH = 480;
// What the conversation keeps, however wide the sidebar is dragged.
const MIN_CONTENT_WIDTH = 320;
const STORAGE_KEY = "steve.sidebarWidth";

export function clampSidebarWidth(width: number, windowWidth: number): number {
	const limit = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, windowWidth - MIN_CONTENT_WIDTH));
	return Math.round(Math.min(limit, Math.max(MIN_WIDTH, width)));
}

/** Browser storage can be unavailable, and then the sidebar just starts at its default width. */
export function loadSidebarWidth(windowWidth: number): number {
	try {
		const saved = Number(localStorage.getItem(STORAGE_KEY));
		return clampSidebarWidth(saved > 0 ? saved : DEFAULT_SIDEBAR_WIDTH, windowWidth);
	} catch {
		return clampSidebarWidth(DEFAULT_SIDEBAR_WIDTH, windowWidth);
	}
}

export function saveSidebarWidth(width: number) {
	try {
		localStorage.setItem(STORAGE_KEY, String(width));
	} catch {
		// Only a convenience.
	}
}
