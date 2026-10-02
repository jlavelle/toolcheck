// toolcheck — lint pi tool calls with the shared toolcheck catalogue.
//
// Intercepts every tool call: spawns `toolcheck check --pi` with the call's
// input, session id, and current context length. Error-severity rules block
// the call; warnings and reminders are appended to the tool result as model-visible text.
// No logic lives here — the CLI owns evaluation; this file only translates
// its envelope into pi actions.
//
// Warnings are delivered on `message_end`, not `tool_result`: pi never fires
// `tool_result` for blocked calls (a `beforeToolCall` block becomes an
// immediate error result that skips the `tool_result` path), but every
// toolResult — allowed, blocked, or errored — ends with `message_end`. The
// returned message keeps the toolResult role (enforced by the runner) and
// replaces the finalized message in place.
//
// Install: copy to ~/.pi/agent/extensions/toolcheck.ts
// Smoke test: pi -e pi/toolcheck.ts

import { execFile } from "node:child_process";

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const BIN = process.env.TOOLCHECK_BIN ?? "toolcheck";
const TIMEOUT_MS = 2000;

const runCheck = (payload: string): Promise<string> =>
  new Promise((resolve, reject) => {
    const child = execFile(
      BIN,
      ["check", "--pi"],
      { timeout: TIMEOUT_MS, maxBuffer: 1024 * 1024, encoding: "utf8" },
      (error, stdout) => (error ? reject(error) : resolve(stdout)),
    );
    // pi ships as a bun-compiled binary whose child_process does not honor
    // execFile's `input` option — write stdin through the child handle.
    if (child.stdin) {
      child.stdin.write(payload);
      child.stdin.end();
    }
  });

interface Envelope {
  decision: "allow" | "deny";
  warnings?: string[];
  errors?: string[];
  reminders?: string[];
}

const rootCallId = (toolCallId: string): string => toolCallId.split("/", 1)[0];

export default function (pi: ExtensionAPI) {
  const hintsByCall = new Map<string, string[]>();
  const notifiedSessions = new Set<string>();

  const addHints = (toolCallId: string, hints: string[]): void => {
    const root = rootCallId(toolCallId);
    const bucket = hintsByCall.get(root) ?? [];
    for (const hint of hints) if (!bucket.includes(hint)) bucket.push(hint);
    if (bucket.length) hintsByCall.set(root, bucket);
  };

  const failOpen = (ctx: ExtensionContext, message: string): void => {
    const id = ctx.sessionManager.getSessionId();
    if (!notifiedSessions.has(id)) {
      notifiedSessions.add(id);
      ctx.ui.notify(`toolcheck unavailable (${message}); allowing call`, "warning");
    }
  };

  pi.on("tool_call", async (event, ctx) => {
    const payload = {
      tool_name: event.toolName,
      tool_input: event.input ?? {},
      session_id: ctx.sessionManager.getSessionId(),
      context_length: ctx.getContextUsage()?.tokens ?? null,
    };

    let stdout: string;
    try {
      stdout = await runCheck(JSON.stringify(payload));
    } catch (error) {
      failOpen(ctx, error instanceof Error ? error.message : String(error));
      return;
    }

    let envelope: Envelope;
    try {
      envelope = JSON.parse(stdout) as Envelope;
      if (envelope.decision !== "allow" && envelope.decision !== "deny") {
        throw new Error(`bad decision: ${String(envelope.decision)}`);
      }
    } catch {
      failOpen(ctx, "unparseable output");
      return;
    }

    addHints(event.toolCallId, [...(envelope.warnings ?? []), ...(envelope.reminders ?? [])]);
    if (envelope.decision === "deny") {
      const reason = (envelope.errors ?? ["(no reason)"]).join("\n\n");
      if (event.parentToolCallId) {
        addHints(event.toolCallId, [`Blocked nested ${event.toolName} call:\n${reason}`]);
      }
      return { block: true, reason };
    }
  });

  pi.on("message_end", (event) => {
    const message = event.message;
    if (message.role !== "toolResult") return;
    const hints = hintsByCall.get(message.toolCallId);
    if (!hints?.length) return;
    hintsByCall.delete(message.toolCallId);
    return {
      message: {
        ...message,
        content: [...message.content, { type: "text", text: hints.join("\n\n") }],
      },
    };
  });
}
