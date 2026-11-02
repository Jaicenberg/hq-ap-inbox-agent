import { createWorkersAI } from "workers-ai-provider";
import { routeAgentRequest } from "agents";
import { AIChatAgent, type OnChatMessageOptions } from "@cloudflare/ai-chat";
import { convertToModelMessages, pruneMessages, stepCountIs, streamText, wrapLanguageModel } from "ai";
import soul from "../agent/SOUL.md?raw";
import agentsMd from "../agent/AGENTS.md?raw";
import { buildTools, skillIndex } from "./tools";

// One Durable Object per chat session: holds the conversation (SQLite) and streams
// answers to the browser over a WebSocket.
export class ChatAgent extends AIChatAgent<Env> {
  maxPersistedMessages = 100;
  chatRecovery = true;

  async onChatMessage(_onFinish: unknown, options?: OnChatMessageOptions) {
    const workersai = createWorkersAI({
      binding: this.env.AI,
      gateway: { id: this.env.AI_GATEWAY_ID } // logs, latency and cost per call
    });

    type ModelId = Parameters<typeof workersai>[0];
    const primary = workersai(this.env.CHAT_MODEL as ModelId, {
      sessionAffinity: this.sessionAffinity, // same replica => prompt-cache hits
      reasoning_effort: this.env.CHAT_REASONING as "low" | "medium" | "high"
    });
    const fallback = workersai(this.env.CHAT_FALLBACK_MODEL as ModelId, {
      sessionAffinity: this.sessionAffinity
    });
    // If the primary model is rate-limited or down, the same step is retried on the fallback model.
    const model = wrapLanguageModel({
      model: primary,
      middleware: {
        specificationVersion: "v3",
        wrapStream: async ({ doStream, params }) => {
          try {
            return await doStream();
          } catch (e) {
            console.warn(`chat model failed, using fallback: ${(e as Error).message}`);
            return fallback.doStream(params);
          }
        }
      }
    });

    const result = streamText({
      model,
      system: `${soul}\n\n${agentsMd}\n\n## Skills (call load_skill first)\n${skillIndex()}\n\nToday is ${new Date().toISOString().slice(0, 10)}; the inbox data ends on 2026-09-30.`,
      messages: pruneMessages({
        messages: await convertToModelMessages(this.messages),
        toolCalls: "before-last-2-messages",
        reasoning: "before-last-message"
      }),
      tools: buildTools(this.env.DB),
      // Evidence first, enforced in code: the first step of every turn must be a tool call,
      // so the model cannot write an answer before it has looked at the data.
      prepareStep: ({ stepNumber }) => (stepNumber === 0 ? { toolChoice: "required" } : undefined),
      stopWhen: stepCountIs(12),
      abortSignal: options?.abortSignal
    });

    // Never fail silently: the user sees why the answer stopped.
    return result.toUIMessageStreamResponse({
      onError: (e) =>
        `The AI model could not finish this answer (${(e as Error)?.message ?? "unknown error"}). Please ask again in a few seconds.`
    });
  }
}

export default {
  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);
    // The inbox holds real vendor data: the chat socket requires the shared access code.
    if (url.pathname.startsWith("/agents/") && url.searchParams.get("code") !== env.ACCESS_CODE) {
      return new Response("Access code required", { status: 401 });
    }
    return (await routeAgentRequest(request, env)) || new Response("Not found", { status: 404 });
  }
} satisfies ExportedHandler<Env>;
