/** Splits a line of arguments the way a shell would: spaces separate, and quotes keep spaces in. */
export function parseArgs(line: string): string[] {
	const args: string[] = [];
	let current: string | null = null;
	let quote: string | null = null;
	for (let index = 0; index < line.length; index++) {
		const char = line[index];
		if (quote) {
			if (char === quote) quote = null;
			else if (char === "\\" && quote === '"' && index + 1 < line.length) current += line[++index];
			else current += char;
		} else if (char === '"' || char === "'") {
			quote = char;
			current ??= "";
		} else if (/\s/.test(char)) {
			if (current !== null) args.push(current);
			current = null;
		} else {
			current = (current ?? "") + char;
		}
	}
	if (quote) throw new Error("An argument has a quote that never closes.");
	if (current !== null) args.push(current);
	return args;
}

/** Reads KEY=VALUE lines. Blank lines are skipped, and a value may hold further = signs. */
export function parseEnv(text: string): Record<string, string> {
	const env: Record<string, string> = {};
	for (const line of text.split("\n")) {
		const trimmed = line.trim();
		if (!trimmed) continue;
		const split = trimmed.indexOf("=");
		if (split < 1) throw new Error(`"${trimmed}" is not KEY=VALUE.`);
		env[trimmed.slice(0, split).trim()] = trimmed.slice(split + 1).trim();
	}
	return env;
}

/** The allowed hosts box: one per line, or separated by commas or spaces. */
export function parseHosts(text: string): string[] {
	return [...new Set(text.split(/[\s,]+/).filter(Boolean))];
}
