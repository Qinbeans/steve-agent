import { isTauri } from "@tauri-apps/api/core";
import { tauriBridge } from "./tauri";
import type { Bridge } from "./types";

export type { AgentSettings, BackendStatus, Bridge, McpAppPage, McpServer, Mode, NewMcpServer, SavedConversation, SavedTurn, Skill, WorkspaceState } from "./types";

/** The real bridge, or in `pnpm dev` in a plain browser the mock. The mock import is dropped from production builds. */
export async function loadBridge(): Promise<Bridge> {
	if (import.meta.env.DEV && !isTauri()) {
		return (await import("./mock")).mockBridge;
	}
	return tauriBridge;
}
