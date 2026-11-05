// Turns an MCP elicitation's schema into form fields, and the answers back into what the schema
// asks for. The spec limits the schema to a flat object of strings, numbers, booleans and choices.

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

export interface Option {
	value: string;
	label: string;
}

export interface Field {
	name: string;
	label: string;
	description: string | null;
	required: boolean;
	kind: "choice" | "multi" | "boolean" | "number" | "integer" | "text";
	options: Option[];
	/** A choice that also accepts something typed in. */
	allowCustom: boolean;
	inputType: "text" | "email" | "url" | "date" | "datetime-local";
	schema: Json;
}

/** What a field holds while being filled in. */
export type Value = string | string[] | boolean;

function optionsFrom(schema: Json): { options: Option[]; allowCustom: boolean } | null {
	if (Array.isArray(schema.enum)) {
		const names: Json[] = schema.enumNames ?? [];
		return {
			options: schema.enum.map((value: Json, index: number) => ({ value: String(value), label: String(names[index] ?? value) })),
			allowCustom: false,
		};
	}
	const variants = schema.oneOf ?? schema.anyOf;
	if (!Array.isArray(variants)) return null;
	const options: Option[] = [];
	let allowCustom = false;
	for (const variant of variants) {
		if (variant.const !== undefined) options.push({ value: String(variant.const), label: String(variant.title ?? variant.const) });
		else if (Array.isArray(variant.enum)) options.push(...variant.enum.map((value: Json) => ({ value: String(value), label: String(value) })));
		else if (variant.type === "string") allowCustom = true;
	}
	return options.length || allowCustom ? { options, allowCustom } : null;
}

const INPUT_TYPES: Record<string, Field["inputType"]> = { email: "email", uri: "url", date: "date", "date-time": "datetime-local" };

export function parseFields(schema: Json): Field[] {
	const properties: Record<string, Json> = schema?.properties ?? {};
	const required: string[] = schema?.required ?? [];
	const names = Object.keys(properties);
	return names.map((name) => {
		const property = properties[name] ?? {};
		const field: Field = {
			name,
			// The prompt already says what a lone field called `value` is for, and libraries title it "Value".
			label: names.length === 1 && name === "value" && (property.title ?? "Value") === "Value" ? "" : (property.title ?? name),
			description: property.description ?? null,
			required: required.includes(name),
			kind: "text",
			options: [],
			allowCustom: false,
			inputType: INPUT_TYPES[property.format] ?? "text",
			schema: property,
		};
		const choices = optionsFrom(property);
		const items = property.type === "array" ? optionsFrom(property.items ?? {}) : null;
		if (items) return { ...field, kind: "multi", ...items };
		if (choices) return { ...field, kind: "choice", ...choices };
		if (property.type === "boolean") return { ...field, kind: "boolean" };
		if (property.type === "number" || property.type === "integer") return { ...field, kind: property.type };
		return field;
	});
}

export function initialValues(fields: Field[]): Record<string, Value> {
	return Object.fromEntries(
		fields.map((field) => {
			const fallback = field.schema.default;
			if (field.kind === "multi") return [field.name, Array.isArray(fallback) ? fallback.map(String) : []];
			if (field.kind === "boolean") return [field.name, fallback === true];
			return [field.name, fallback === undefined ? "" : String(fallback)];
		}),
	);
}

function checkString(field: Field, text: string): string | null {
	const { minLength, maxLength, pattern } = field.schema;
	if (minLength !== undefined && text.length < minLength) return `Use at least ${minLength} characters.`;
	if (maxLength !== undefined && text.length > maxLength) return `Use at most ${maxLength} characters.`;
	if (pattern) {
		try {
			if (!new RegExp(pattern, "u").test(text)) return "That is not in the form it needs to be.";
		} catch {
			// A pattern this browser cannot read is for the server to check.
		}
	}
	if (field.inputType === "email" && !/^[^@\s]+@[^@\s]+$/.test(text)) return "Enter an email address.";
	if (field.inputType === "url" && !URL.canParse(text)) return "Enter a full web address.";
	return null;
}

/** The content to send, or what is wrong with each field. */
export function collect(fields: Field[], values: Record<string, Value>): { content: Record<string, Json>; errors: Record<string, string> } {
	const content: Record<string, Json> = {};
	const errors: Record<string, string> = {};
	for (const field of fields) {
		const value = values[field.name];
		if (field.kind === "boolean") {
			content[field.name] = value === true;
			continue;
		}
		if (field.kind === "multi") {
			const picked = value as string[];
			const { minItems, maxItems } = field.schema;
			if (field.required && !picked.length) errors[field.name] = "Choose at least one.";
			else if (minItems !== undefined && picked.length < minItems) errors[field.name] = `Choose at least ${minItems}.`;
			else if (maxItems !== undefined && picked.length > maxItems) errors[field.name] = `Choose at most ${maxItems}.`;
			else if (picked.length || field.required) content[field.name] = picked;
			continue;
		}
		const text = String(value).trim();
		if (!text) {
			if (field.required) errors[field.name] = field.kind === "choice" ? "Choose one." : "This is required.";
			continue;
		}
		if (field.kind === "number" || field.kind === "integer") {
			const number = Number(text);
			if (!Number.isFinite(number) || (field.kind === "integer" && !Number.isInteger(number))) {
				errors[field.name] = field.kind === "integer" ? "Enter a whole number." : "Enter a number.";
			} else if (field.schema.minimum !== undefined && number < field.schema.minimum) {
				errors[field.name] = `Use ${field.schema.minimum} or more.`;
			} else if (field.schema.maximum !== undefined && number > field.schema.maximum) {
				errors[field.name] = `Use ${field.schema.maximum} or less.`;
			} else content[field.name] = number;
			continue;
		}
		if (field.kind === "choice") {
			const known = field.options.some((option) => option.value === text);
			if (!known && !field.allowCustom) {
				errors[field.name] = "Choose one of the options.";
				continue;
			}
			// A number or boolean choice goes back as what the schema listed, not as text.
			const raw = (field.schema.enum ?? []).find((candidate: Json) => String(candidate) === text);
			content[field.name] = raw !== undefined ? raw : text;
			continue;
		}
		const problem = checkString(field, text);
		if (problem) errors[field.name] = problem;
		else content[field.name] = text;
	}
	return { content, errors };
}
