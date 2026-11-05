import { describe, expect, it } from "vitest";
import { clampSidebarWidth } from "./sidebar-width";

describe("clampSidebarWidth", () => {
	it("keeps the width within its limits", () => {
		expect(clampSidebarWidth(300, 1200)).toBe(300);
		expect(clampSidebarWidth(20, 1200)).toBe(180);
		expect(clampSidebarWidth(2000, 1200)).toBe(480);
	});
	it("leaves room for the conversation in a small window", () => {
		expect(clampSidebarWidth(480, 700)).toBe(380);
	});
	it("never goes below the minimum, however small the window", () => {
		expect(clampSidebarWidth(480, 400)).toBe(180);
	});
});
