import { describe, expect, it } from "vitest";
import { parseArgs, parseEnv, parseHosts } from "./addons";

describe("parseArgs", () => {
	it("splits on spaces and keeps quoted spaces", () => {
		expect(parseArgs(`run  server.py --name "my server" 'a b'`)).toEqual(["run", "server.py", "--name", "my server", "a b"]);
	});
	it("keeps empty quoted arguments and escaped quotes", () => {
		expect(parseArgs(`"" "say \\"hi\\""`)).toEqual(["", `say "hi"`]);
	});
	it("is empty for a blank line, and refuses an unclosed quote", () => {
		expect(parseArgs("   ")).toEqual([]);
		expect(() => parseArgs(`"oops`)).toThrow("quote");
	});
});

describe("parseEnv", () => {
	it("reads lines, skipping blanks, and keeps = in values", () => {
		expect(parseEnv("A=1\n\n B = x=y \n")).toEqual({ A: "1", B: "x=y" });
	});
	it("refuses anything else", () => {
		expect(() => parseEnv("nope")).toThrow("KEY=VALUE");
		expect(() => parseEnv("=1")).toThrow("KEY=VALUE");
	});
});

describe("parseHosts", () => {
	it("takes lines, commas and spaces, once each", () => {
		expect(parseHosts("nas.local\n10.0.0.0/8, *.home.arpa  nas.local\n\n")).toEqual(["nas.local", "10.0.0.0/8", "*.home.arpa"]);
	});
	it("gives nothing for an empty box", () => {
		expect(parseHosts("  \n ")).toEqual([]);
	});
});
