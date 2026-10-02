// Deterministic harness for pi/toolcheck.ts.
//
// The extension's only runtime import is node:child_process (the pi types are
// type-only), so the module loads without pi. TOOLCHECK_BIN points at a stub
// binary whose reply per call id is baked into the script.
//
// Run: bun test pi/toolcheck.test.ts

import { afterAll, describe, expect, test } from "bun:test";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const tmp = mkdtempSync(join(tmpdir(), "toolcheck-test-"));
const bin = join(tmp, "fake-toolcheck");
process.env.TOOLCHECK_BIN = bin; // read at module load; must precede the import

const { default: makeExtension } = await import("./toolcheck.ts");

type Envelope =
  | { decision: "allow" | "deny"; warnings?: string[]; errors?: string[]; reminders?: string[] }
  | "fail";
type Handler = (event: any, ctx: any) => any;

const makePi = () => {
  const handlers = new Map<string, Handler[]>();
  const api = {
    on: (name: string, handler: Handler) => {
      const list = handlers.get(name) ?? [];
      list.push(handler);
      handlers.set(name, list);
    },
  };
  return { api, handlers };
};

const fire = async (handlers: Map<string, Handler[]>, name: string, event: any, ctx: any) => {
  let result: any;
  for (const handler of handlers.get(name) ?? []) result = await handler(event, ctx);
  return result;
};

const mkCtx = () => ({
  sessionManager: { getSessionId: () => "test-session" },
  getContextUsage: () => ({ tokens: 42 }),
  ui: { notify: () => {} },
});

const toolResultMessage = (id: string, text = "original") => ({
  role: "toolResult",
  toolCallId: id,
  toolName: "bash",
  content: [{ type: "text", text }],
  isError: false,
  timestamp: 0,
});

// The stub reads the payload from stdin and answers each call (tool name tN)
// with the envelope at index N; "fail" exits non-zero. The payload carries no
// toolCallId, so the tests key the fixture by tool_name.
const setStub = (envelopes: Envelope[]) => {
  writeFileSync(
    bin,
    `#!/usr/bin/env node
let s = "";
process.stdin.on("data", (c) => (s += c));
process.stdin.on("end", () => {
  const envs = ${JSON.stringify(envelopes)};
  const env = envs[Number(JSON.parse(s).tool_name.slice(1))];
  if (env === "fail") {
    console.error("stub failure");
    process.exit(1);
  }
  process.stdout.write(JSON.stringify(env));
});`,
  );
  chmodSync(bin, 0o755);
};

const lcg = (seed: number) => () => {
  seed = (seed * 1103515245 + 12345) % 2 ** 31;
  return seed / 2 ** 31;
};

afterAll(() => rmSync(tmp, { recursive: true, force: true }));

describe("pi/toolcheck.ts", () => {
  test("allow + warnings: message_end appends them once, input untouched", async () => {
    setStub([{ decision: "allow", warnings: ["w1", "w2"] }]);
    const { api, handlers } = makePi();
    makeExtension(api);
    const ctx = mkCtx();

    expect(
      await fire(handlers, "tool_call", { toolName: "t0", toolCallId: "c0", input: {} }, ctx),
    ).toBeUndefined();

    const message = toolResultMessage("c0");
    const patch = await fire(handlers, "message_end", { message }, ctx);
    expect(patch?.message?.role).toBe("toolResult"); // same-role requirement of the runner
    expect(patch?.message?.toolCallId).toBe("c0");
    expect(patch?.message?.content).toHaveLength(2);
    expect(patch?.message?.content[1]).toEqual({ type: "text", text: "w1\n\nw2" });
    expect(message.content).toHaveLength(1); // the passed message is not mutated

    // consumed: a second message_end for the same id delivers nothing
    expect(await fire(handlers, "message_end", { message: toolResultMessage("c0") }, ctx)).toBeUndefined();
  });

  test("allow + warnings + reminders: both appended in order", async () => {
    setStub([{ decision: "allow", warnings: ["w"], reminders: ["r"] }]);
    const { api, handlers } = makePi();
    makeExtension(api);
    const ctx = mkCtx();

    await fire(handlers, "tool_call", { toolName: "t0", toolCallId: "c0", input: {} }, ctx);

    const patch = await fire(handlers, "message_end", { message: toolResultMessage("c0") }, ctx);
    expect(patch?.message?.content).toHaveLength(2);
    expect(patch?.message?.content[1]).toEqual({ type: "text", text: "w\n\nr" });
  });

  test("deny + warnings: call blocked, message_end on the error toolResult still delivers", async () => {
    setStub([{ decision: "deny", warnings: ["w"], errors: ["[e1] no"] }]);
    const { api, handlers } = makePi();
    makeExtension(api);
    const ctx = mkCtx();

    const blocked = await fire(
      handlers,
      "tool_call",
      { toolName: "t0", toolCallId: "c0", input: {} },
      ctx,
    );
    expect(blocked).toEqual({ block: true, reason: "[e1] no" });

    const patch = await fire(handlers, "message_end", { message: toolResultMessage("c0", "blocked") }, ctx);
    expect(patch?.message?.content).toHaveLength(2);
    expect(patch?.message?.content[1]).toEqual({ type: "text", text: "w" });

    expect(await fire(handlers, "message_end", { message: toolResultMessage("c0") }, ctx)).toBeUndefined();
  });

  test("message_end for other roles or unknown ids: no result, no mutation", async () => {
    setStub([{ decision: "allow", warnings: ["w"] }]);
    const { api, handlers } = makePi();
    makeExtension(api);
    const ctx = mkCtx();

    await fire(handlers, "tool_call", { toolName: "t0", toolCallId: "c0", input: {} }, ctx);

    const assistant = { role: "assistant", toolCallId: "c0", content: [{ type: "text", text: "hi" }] };
    expect(await fire(handlers, "message_end", { message: assistant }, ctx)).toBeUndefined();
    expect(assistant.content).toHaveLength(1);

    const stray = toolResultMessage("nope");
    expect(await fire(handlers, "message_end", { message: stray }, ctx)).toBeUndefined();
    expect(stray.content).toHaveLength(1);

    // warnings still parked for c0, untouched by the misses above
    const patch = await fire(handlers, "message_end", { message: toolResultMessage("c0") }, ctx);
    expect(patch?.message?.content[1]).toEqual({ type: "text", text: "w" });
  });

  test("nested calls: hints delivered on the parent's toolResult, none on the nested id", async () => {
    setStub([{ decision: "allow" }, { decision: "allow", warnings: ["w1"] }, { decision: "allow", reminders: ["r2"] }]);
    const { api, handlers } = makePi();
    makeExtension(api);
    const ctx = mkCtx();

    await fire(handlers, "tool_call", { toolName: "t0", toolCallId: "c0", input: {} }, ctx);
    await fire(handlers, "tool_call", { toolName: "t1", toolCallId: "c0/1", parentToolCallId: "c0", input: {} }, ctx);
    await fire(handlers, "tool_call", { toolName: "t2", toolCallId: "c0/1/1", parentToolCallId: "c0/1", input: {} }, ctx);

    expect(await fire(handlers, "message_end", { message: toolResultMessage("c0/1") }, ctx)).toBeUndefined();

    const patch = await fire(handlers, "message_end", { message: toolResultMessage("c0") }, ctx);
    expect(patch?.message?.content).toHaveLength(2);
    expect(patch?.message?.content[1]).toEqual({ type: "text", text: "w1\n\nr2" });
    expect(await fire(handlers, "message_end", { message: toolResultMessage("c0") }, ctx)).toBeUndefined();
  });

  test("nested deny: blocked, and the reason surfaces on the parent even if the script swallowed it", async () => {
    setStub([{ decision: "allow" }, { decision: "deny", errors: ["[e1] no"] }, { decision: "deny", errors: ["[e1] no"] }]);
    const { api, handlers } = makePi();
    makeExtension(api);
    const ctx = mkCtx();

    await fire(handlers, "tool_call", { toolName: "t0", toolCallId: "c0", input: {} }, ctx);
    const nested = { toolName: "t1", toolCallId: "c0/1", parentToolCallId: "c0", input: {} };
    expect(await fire(handlers, "tool_call", nested, ctx)).toEqual({ block: true, reason: "[e1] no" });
    await fire(handlers, "tool_call", { ...nested, toolName: "t2", toolCallId: "c0/2" }, ctx);

    const patch = await fire(handlers, "message_end", { message: toolResultMessage("c0") }, ctx);
    expect(patch?.message?.content[1]).toEqual({
      type: "text",
      text: "Blocked nested t1 call:\n[e1] no\n\nBlocked nested t2 call:\n[e1] no",
    });
  });

  test("top-level deny: reason is not repeated as a hint", async () => {
    setStub([{ decision: "deny", errors: ["[e1] no"] }]);
    const { api, handlers } = makePi();
    makeExtension(api);
    const ctx = mkCtx();

    await fire(handlers, "tool_call", { toolName: "t0", toolCallId: "c0", input: {} }, ctx);
    expect(await fire(handlers, "message_end", { message: toolResultMessage("c0") }, ctx)).toBeUndefined();
  });

  test("property: over random call trees every distinct hint reaches its root exactly once", async () => {
    const rand = lcg(20261002);
    const n = 80;
    const warningsOf = Array.from({ length: n }, (_, i) => (rand() < 0.5 ? [`w${i % 5}`] : []));
    setStub(warningsOf.map((warnings) => ({ decision: "allow", warnings })));
    const { api, handlers } = makePi();
    makeExtension(api);
    const ctx = mkCtx();

    const ids: string[] = [];
    const expected = new Map<string, string[]>();
    for (let i = 0; i < n; i++) {
      const parent = ids.length && rand() < 0.7 ? ids[Math.floor(rand() * ids.length)] : undefined;
      const id = parent ? `${parent}/${i}` : `c${i}`;
      ids.push(id);
      await fire(
        handlers,
        "tool_call",
        { toolName: `t${i}`, toolCallId: id, input: {}, ...(parent ? { parentToolCallId: parent } : {}) },
        ctx,
      );
      const root = id.split("/")[0];
      const bucket = expected.get(root) ?? [];
      for (const hint of warningsOf[i]) if (!bucket.includes(hint)) bucket.push(hint);
      expected.set(root, bucket);
    }

    for (const id of ids) {
      if (id.includes("/")) {
        expect(await fire(handlers, "message_end", { message: toolResultMessage(id) }, ctx)).toBeUndefined();
      }
    }
    for (const [root, hints] of expected) {
      const patch = await fire(handlers, "message_end", { message: toolResultMessage(root) }, ctx);
      if (hints.length) expect(patch?.message?.content[1]).toEqual({ type: "text", text: hints.join("\n\n") });
      else expect(patch).toBeUndefined();
      expect(await fire(handlers, "message_end", { message: toolResultMessage(root) }, ctx)).toBeUndefined();
    }
  });

  test("stub exits non-zero: fail open", async () => {
    setStub(["fail"]);
    const { api, handlers } = makePi();
    makeExtension(api);
    const ctx = mkCtx();

    expect(
      await fire(handlers, "tool_call", { toolName: "t0", toolCallId: "c0", input: {} }, ctx),
    ).toBeUndefined();
  });

  test("mixed allow/deny/fail: each warning list delivered exactly once", async () => {
    const rand = lcg(20260826);
    const n = 60;
    const envelopes: Envelope[] = Array.from({ length: n }, () => {
      const roll = rand();
      if (roll < 0.2) return "fail";
      const decision = roll < 0.6 ? "deny" : "allow";
      const warnings = Array.from({ length: 1 + Math.floor(rand() * 3) }, (_, i) => `w${i}`);
      return decision === "deny"
        ? { decision, warnings, errors: ["[e]"] }
        : { decision, warnings };
    });
    setStub(envelopes);
    const { api, handlers } = makePi();
    makeExtension(api);
    const ctx = mkCtx();

    for (let i = 0; i < n; i++) {
      const id = `c${i}`;
      const envelope = envelopes[i];
      const result = await fire(handlers, "tool_call", { toolName: `t${i}`, toolCallId: id, input: {} }, ctx);
      if (envelope === "fail") expect(result).toBeUndefined();
      else if (envelope.decision === "deny") expect(result).toEqual({ block: true, reason: "[e]" });
      else expect(result).toBeUndefined();

      const message = toolResultMessage(id, `out-${i}`);
      const patch = await fire(handlers, "message_end", { message }, ctx);
      const warnings = envelope === "fail" ? undefined : envelope.warnings;
      if (warnings?.length) {
        expect(patch?.message?.content).toHaveLength(2);
        expect(patch?.message?.content[1]).toEqual({ type: "text", text: warnings.join("\n\n") });
      } else {
        expect(patch).toBeUndefined();
      }
      expect(message.content).toHaveLength(1);

      // consumed: a second message_end for the same id delivers nothing
      expect(await fire(handlers, "message_end", { message: toolResultMessage(id) }, ctx)).toBeUndefined();
    }
  });
});
