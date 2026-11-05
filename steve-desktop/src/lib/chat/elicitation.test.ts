import { describe, expect, it } from "vitest";
import { collect, initialValues, parseFields } from "./elicitation";

const single = (property: unknown, required = true) => ({ properties: { pick: property }, required: required ? ["pick"] : [] });

describe("parseFields", () => {
	it("reads every way a schema can list choices", () => {
		const titled = parseFields(single({ oneOf: [{ const: "r", title: "Red" }, { const: "g", title: "Green" }] }))[0];
		expect(titled.kind).toBe("choice");
		expect(titled.options).toEqual([{ value: "r", label: "Red" }, { value: "g", label: "Green" }]);
		expect(parseFields(single({ type: "string", enum: ["a", "b"] }))[0].options.map((o) => o.value)).toEqual(["a", "b"]);
		expect(parseFields(single({ enum: ["a"], enumNames: ["Apple"] }))[0].options[0].label).toBe("Apple");
	});

	it("sees choices that also accept custom input", () => {
		const field = parseFields(single({ anyOf: [{ const: "a" }, { type: "string" }] }))[0];
		expect(field.kind).toBe("choice");
		expect(field.allowCustom).toBe(true);
		expect(parseFields(single({ enum: ["a"] }))[0].allowCustom).toBe(false);
	});

	it("reads multiple choice, and plain inputs", () => {
		expect(parseFields(single({ type: "array", items: { enum: ["a", "b"] } }))[0].kind).toBe("multi");
		expect(parseFields(single({ type: "array", items: { anyOf: [{ const: "a", title: "A" }] } }))[0].options[0].label).toBe("A");
		expect(parseFields(single({ type: "boolean" }))[0].kind).toBe("boolean");
		expect(parseFields(single({ type: "integer" }))[0].kind).toBe("integer");
		expect(parseFields(single({ type: "string", format: "email" }))[0].inputType).toBe("email");
		expect(parseFields(single({ type: "string" }))[0].kind).toBe("text");
	});

	it("labels fields, leaving a lone `value` to the prompt", () => {
		expect(parseFields({ properties: { value: { enum: ["a"] } } })[0].label).toBe("");
		expect(parseFields({ properties: { value: { title: "Value", enum: ["a"] } } })[0].label).toBe("");
		expect(parseFields({ properties: { value: { title: "Colour", enum: ["a"] } } })[0].label).toBe("Colour");
		expect(parseFields({ properties: { title: { type: "string", title: "Ticket title" } } })[0].label).toBe("Ticket title");
		expect(parseFields({ properties: { title: { type: "string" } } })[0].label).toBe("title");
	});

	it("copes with an empty or missing schema", () => {
		expect(parseFields({})).toEqual([]);
		expect(parseFields(undefined)).toEqual([]);
	});
});

describe("initialValues", () => {
	it("uses a schema's defaults", () => {
		const fields = parseFields({ properties: { a: { type: "string", default: "x" }, b: { type: "boolean", default: true }, c: { type: "array", items: { enum: ["p"] }, default: ["p"] }, d: { type: "integer", default: 2 } } });
		expect(initialValues(fields)).toEqual({ a: "x", b: true, c: ["p"], d: "2" });
	});
});

describe("collect", () => {
	const run = (property: unknown, value: unknown, required = true) => collect(parseFields(single(property, required)), { pick: value as string });

	it("requires what is required, and leaves out empty optional fields", () => {
		expect(run({ type: "string" }, "").errors.pick).toBe("This is required.");
		expect(run({ enum: ["a"] }, "").errors.pick).toBe("Choose one.");
		expect(run({ type: "string" }, "  ", false).content).toEqual({});
	});

	it("converts numbers, and checks their limits", () => {
		expect(run({ type: "integer" }, "3").content.pick).toBe(3);
		expect(run({ type: "integer" }, "3.5").errors.pick).toBe("Enter a whole number.");
		expect(run({ type: "number" }, "3.5").content.pick).toBe(3.5);
		expect(run({ type: "number" }, "abc").errors.pick).toBe("Enter a number.");
		expect(run({ type: "number", minimum: 5 }, "3").errors.pick).toBe("Use 5 or more.");
		expect(run({ type: "number", maximum: 2 }, "3").errors.pick).toBe("Use 2 or less.");
	});

	it("checks strings", () => {
		expect(run({ type: "string", minLength: 3 }, "ab").errors.pick).toContain("at least 3");
		expect(run({ type: "string", maxLength: 1 }, "ab").errors.pick).toContain("at most 1");
		expect(run({ type: "string", pattern: "^a" }, "b").errors.pick).toBeTruthy();
		expect(run({ type: "string", pattern: "(" }, "b").content.pick).toBe("b");
		expect(run({ type: "string", format: "email" }, "nope").errors.pick).toBeTruthy();
		expect(run({ type: "string", format: "uri" }, "https://a.example").content.pick).toBe("https://a.example");
	});

	it("only takes custom text where the schema allows it", () => {
		expect(run({ enum: ["a"] }, "other").errors.pick).toBeTruthy();
		expect(run({ anyOf: [{ const: "a" }, { type: "string" }] }, "other").content.pick).toBe("other");
	});

	it("gives a choice back as the schema's own value", () => {
		expect(run({ enum: [1, 2] }, "2").content.pick).toBe(2);
	});

	it("handles booleans and multiple choice", () => {
		expect(run({ type: "boolean" }, true).content.pick).toBe(true);
		expect(run({ type: "boolean" }, false).content.pick).toBe(false);
		const multi = { type: "array", items: { enum: ["a", "b"] }, minItems: 1, maxItems: 1 };
		expect(run(multi, []).errors.pick).toBe("Choose at least one.");
		expect(run(multi, ["a", "b"]).errors.pick).toBe("Choose at most 1.");
		expect(run(multi, ["a"]).content.pick).toEqual(["a"]);
	});
});
