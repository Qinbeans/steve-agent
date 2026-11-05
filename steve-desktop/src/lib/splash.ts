// The splash itself is in app.html, so that it shows before any of the app has loaded.

/** What the agent's start is waiting on. MCP servers are the slow part, so they get named. */
export function splashMessage(mcpServers: number): string {
	if (mcpServers === 0) return "Starting the agent…";
	return `Starting the agent and connecting to ${mcpServers === 1 ? "1 MCP server" : `${mcpServers} MCP servers`}…`;
}

export function setSplashStatus(text: string) {
	const status = document.getElementById("splash-status");
	if (status) status.textContent = text;
}

export function hideSplash() {
	const splash = document.getElementById("splash");
	if (!splash) return;
	splash.classList.add("leaving");
	setTimeout(() => splash.remove(), 200);
}
