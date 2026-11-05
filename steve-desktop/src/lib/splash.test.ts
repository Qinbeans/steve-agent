import { describe, expect, it } from "vitest";
import { splashMessage } from "./splash";

describe("splashMessage", () => {
	it("names MCP servers when there are some", () => {
		expect(splashMessage(0)).toBe("Starting the agent…");
		expect(splashMessage(1)).toBe("Starting the agent and connecting to 1 MCP server…");
		expect(splashMessage(3)).toBe("Starting the agent and connecting to 3 MCP servers…");
	});
});
