import { describe, expect, it } from "vitest";
import { applyEvent, continuationId, endStream, latestContext, canRetry, newTurn, restoredTurn, turnText, type Turn } from "./turn";

function play(events: unknown[]): Turn {
	const turn = newTurn("hi");
	for (const event of events) applyEvent(turn, event);
	return turn;
}

const message = (id: string, text: string, status = "completed") => ({
	type: "message",
	id,
	status,
	content: [{ type: "output_text", text }],
});

describe("applyEvent", () => {
	it("streams text and takes the response id from the first event", () => {
		const turn = play([
			{ type: "response.created", response: { id: "r1" } },
			{ type: "response.output_item.added", item: { type: "message", id: "m1", status: "in_progress", content: [] } },
			{ type: "response.output_text.delta", item_id: "m1", delta: "Hel" },
			{ type: "response.output_text.delta", item_id: "m1", delta: "lo" },
		]);
		expect(turn.responseId).toBe("r1");
		expect(turn.status).toBe("streaming");
		expect(turn.items).toMatchObject([{ kind: "message", text: "Hello", status: "in_progress" }]);
	});

	it("tracks a tool from arguments through its result, and keeps the subagent link", () => {
		const added = { type: "mcp_call", id: "t1", name: "task", arguments: "", status: "in_progress", child_response_id: "r2" };
		const turn = play([
			{ type: "response.output_item.added", item: added },
			{ type: "response.mcp_call_arguments.delta", item_id: "t1", delta: '{"a":1}' },
		]);
		expect(turn.items[0]).toMatchObject({ kind: "tool", args: '{"a":1}', childId: "r2", status: "in_progress" });
		applyEvent(turn, {
			type: "response.output_item.done",
			item: { ...added, arguments: '{"a":1}', output: "done", status: "completed" },
		});
		expect(turn.items[0]).toMatchObject({ output: "done", status: "completed", childId: "r2" });
	});

	it("reads a tool's structured error", () => {
		const item = {
			type: "mcp_call",
			id: "t1",
			name: "ls",
			arguments: "{}",
			status: "failed",
			error: { type: "mcp_tool_execution_error", content: [{ type: "text", text: "no such path" }] },
		};
		expect(play([{ type: "response.output_item.added", item }]).items[0]).toMatchObject({ error: "no such path" });
	});

	it("drops partial output from a retried attempt when the terminal event arrives", () => {
		const turn = play([
			{ type: "response.output_item.added", item: message("m1", "", "in_progress") },
			{ type: "response.output_text.delta", item_id: "m1", delta: "Half an ans" },
			{ type: "response.output_item.done", item: message("m1", "Half an ans", "incomplete") },
			{ type: "response.output_item.added", item: message("m2", "", "in_progress") },
			{ type: "response.output_text.delta", item_id: "m2", delta: "Whole answer." },
			{
				type: "response.completed",
				response: { id: "r1", status: "completed", output: [message("m2", "Whole answer.")], usage: { total_tokens: 9 } },
			},
		]);
		expect(turn.items).toHaveLength(1);
		expect(turn.items[0]).toMatchObject({ text: "Whole answer." });
		expect(turn.totalTokens).toBe(9);
	});

	it("reports failure with its partial output", () => {
		const turn = play([
			{
				type: "response.failed",
				response: { id: "r1", status: "failed", output: [message("m1", "Part", "incomplete")], error: { message: "Boom" } },
			},
		]);
		expect(turn.status).toBe("failed");
		expect(turn.error).toBe("Boom");
		expect(turn.items).toHaveLength(1);
	});

	it("streams reasoning", () => {
		const turn = play([
			{ type: "response.output_item.added", item: { type: "reasoning", id: "rs", status: "in_progress", summary: [] } },
			{ type: "response.reasoning_summary_text.delta", item_id: "rs", delta: "Hm" },
			{ type: "response.reasoning_summary_text.delta", item_id: "rs", delta: "m." },
		]);
		expect(turn.items[0]).toMatchObject({ kind: "reasoning", text: "Hmm.", status: "in_progress" });
	});

	it("ignores unknown events and items", () => {
		const turn = play([
			{ type: "response.something_new" },
			{ type: "response.output_item.added", item: { type: "web_search_call", id: "x" } },
			{ type: "response.output_text.delta", item_id: "nope", delta: "x" },
		]);
		expect(turn.items).toEqual([]);
	});
});

describe("endStream", () => {
	it("does not override a terminal event", () => {
		const turn = play([{ type: "response.completed", response: { id: "r", status: "completed", output: [] } }]);
		endStream(turn, false);
		expect(turn.status).toBe("completed");
	});

	it("marks a stopped turn cancelled and closes running tools", () => {
		const turn = play([{ type: "response.output_item.added", item: { type: "mcp_call", id: "t", name: "ls", status: "in_progress" } }]);
		endStream(turn, true);
		expect(turn.status).toBe("cancelled");
		expect(turn.items[0].status).toBe("incomplete");
	});

	it("explains a stream that just ended, and shows bridge errors", () => {
		const ended = newTurn("x");
		endStream(ended, false);
		expect(ended.status).toBe("failed");
		expect(ended.error).toContain("connection");
		const refused = newTurn("x");
		endStream(refused, false, "conversation too large");
		expect(refused.error).toBe("conversation too large");
	});
});

describe("continuationId", () => {
	const turn = (status: Turn["status"], responseId: string): Turn => ({ ...newTurn("x"), status, responseId });

	it("continues from the last finished turn, skipping failed and cancelled ones", () => {
		expect(continuationId([turn("completed", "a"), turn("failed", "b"), turn("cancelled", "c")])).toBe("a");
		expect(continuationId([turn("completed", "a"), turn("incomplete", "b")])).toBe("b");
		expect(continuationId([turn("failed", "a")])).toBeNull();
		expect(continuationId([])).toBeNull();
	});
});

describe("latestContext", () => {
	const context = { used: 10, limit: 100, compacts_at: 85, breakdown: [] };

	it("takes it from the terminal event, and skips turns without one", () => {
		const reported = play([{ type: "response.completed", response: { id: "r", status: "completed", output: [], context } }]);
		const failed = newTurn("x");
		endStream(failed, false);
		expect(latestContext([reported, failed])).toEqual(context);
		expect(latestContext([failed])).toBeNull();
	});
});

describe("turnText", () => {
	it("joins the agent's messages, leaving out tools and reasoning", () => {
		const turn = newTurn("hi");
		turn.items.push(
			{ kind: "reasoning", id: "r", text: "thinking", status: "completed" },
			{ kind: "message", id: "a", text: "First **part**", refusal: "", status: "completed" },
			{ kind: "tool", id: "t", name: "ls", args: "", output: "files", error: null, status: "completed", childId: null, app: null, appResult: null },
			{ kind: "message", id: "b", text: "Second", refusal: "", status: "completed" },
		);
		expect(turnText(turn)).toBe("First **part**\n\nSecond");
	});
});

describe("restoredTurn", () => {
	const message = { type: "message", id: "m", status: "completed", content: [{ type: "output_text", text: "Saved." }] };
	const context = { used: 5, limit: 10, compacts_at: 8, breakdown: [] };

	it("rebuilds a finished turn from what the server saved", () => {
		const turn = restoredTurn({
			response: { id: "resp_1", status: "completed", output: [message], usage: { total_tokens: 9 }, context },
			input_items: [{ type: "message", role: "user", content: [{ type: "input_text", text: "Hi" }, { type: "input_file", filename: "a.pdf" }] }],
			compaction: false,
		});
		expect(turn.input).toBe("Hi");
		expect(turn.attachments).toEqual(["a.pdf"]);
		expect(turn.status).toBe("completed");
		expect(turn.responseId).toBe("resp_1");
		expect(turnText(turn)).toBe("Saved.");
		expect(turn.context).toEqual(context);
		expect(continuationId([turn])).toBe("resp_1");
	});

	it("keeps a failure as a failure, with its message", () => {
		const turn = restoredTurn({
			response: { id: "resp_2", status: "failed", output: [], error: { code: "x", message: "It broke." } },
			input_items: [],
			compaction: false,
		});
		expect(turn.status).toBe("failed");
		expect(turn.error).toContain("It broke.");
		expect(continuationId([turn])).toBeNull();
	});

	it("marks a compaction, which has no input", () => {
		const turn = restoredTurn({ response: { id: "r", status: "completed", output: [message] }, input_items: [], compaction: true });
		expect(turn.compaction).toBe(true);
		expect(turn.input).toBe("");
	});

	it("cannot retry saved files, whose paths are not known", () => {
		const saved = { response: { id: "r", status: "completed", output: [] }, compaction: false };
		const withFile = restoredTurn({ ...saved, input_items: [{ content: [{ type: "input_file", filename: "a.pdf" }] }] });
		expect(canRetry(withFile)).toBe(false);
		expect(canRetry(restoredTurn({ ...saved, input_items: [{ content: [{ type: "input_text", text: "Hi" }] }] }))).toBe(true);
		expect(canRetry(newTurn("Hi", ["a.pdf"], false, ["/a.pdf"]))).toBe(true);
	});
});

describe("questions", () => {
	it("opens on a request and closes when resolved", () => {
		const turn = newTurn("hi");
		applyEvent(turn, { type: "steve.approval.requested", sequence_number: 3, id: "a", tool: "write_file", arguments: { file_path: "/x" } });
		applyEvent(turn, { type: "steve.elicitation.requested", sequence_number: 4, id: "b", server: "s", mode: "form", message: "Pick", schema: {} });
		expect(turn.questions.map((question) => [question.id, question.kind])).toEqual([["a", "approval"], ["b", "elicitation"]]);
		expect(turn.questions[0].tool).toBe("write_file");
		applyEvent(turn, { type: "steve.interaction.resolved", sequence_number: 5, id: "a" });
		expect(turn.questions.map((question) => question.id)).toEqual(["b"]);
	});
});
