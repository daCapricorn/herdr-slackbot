import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { Worker } from "./worker.js";
import { code, deliver, eventPayload, statusPayload } from "./presentation.js";

export function authorized(config, ctx) {
  return (ctx.isAuthorizedSender ?? ctx.auth?.isAuthorizedSender) === true
    && ctx.channel === config.route.channel
    && ctx.accountId === config.route.accountId
    && config.ownerIds.includes(ctx.senderId);
}

export default {
  id: "herdr-bridge", name: "Herdr Bridge", version: "0.2.0",
  register(api) {
    let config = api.pluginConfig;
    let worker, timer, busy = false, stopping = true, controller, pumping;
    const call = (method, params) => {
      if (!worker || stopping) throw new Error("Herdr bridge service is not running");
      return worker.call(method, params);
    };
    const allowed = (ctx) => {
      if (!authorized(config, ctx)) throw new Error("This channel/account/sender is not authorized for Herdr");
      ctx.assertOwnerCurrent?.();
    };
    api.registerService({
      id: "herdr-bridge",
      reload: { configPrefixes: ["plugins.entries.herdr-bridge.config"] },
      async start(ctx) {
        config = ctx.config.plugins?.entries?.["herdr-bridge"]?.config ?? api.pluginConfig;
        const stateDir = join(ctx.stateDir, "herdr-bridge");
        await mkdir(stateDir, { recursive: true, mode: 0o700 });
        stopping = false;
        controller = new AbortController();
        const startWorker = () => {
          worker = new Worker(config, join(stateDir, "bridge.sqlite"), api.logger);
          worker.start();
        };
        startWorker();
        const pump = async () => {
          if (busy || stopping) return;
          busy = true;
          try {
            if (worker.spawnFailed || worker.child.exitCode !== null) {
              await worker.stop();
              if (stopping) return;
              startWorker();
            }
            const events = await call("events");
            for (const event of events) {
              if (stopping) break;
              await deliver(api, config.route, eventPayload(event), controller.signal);
              await call("ack", { event_id: event.id });
              api.logger.info(`Herdr event delivered: ${event.type} ${event.id}`);
            }
            ctx.serviceHealth?.clearFailure();
          } catch (error) {
            if (!stopping) { api.logger.warn(`Herdr delivery deferred: ${error.message}`); ctx.serviceHealth?.reportFailure(error); }
          } finally { busy = false; }
        };
        const tick = () => { if (!busy && !stopping) pumping = pump(); };
        timer = setInterval(tick, 5000);
        timer.unref();
        tick();
      },
      async stop() {
        stopping = true;
        clearInterval(timer);
        controller?.abort();
        await pumping;
        await worker?.stop();
        worker = undefined;
      },
    });
    api.registerCommand({
      name: "herdr", description: "Herdr status, read, pending, send, and exact approvals",
      acceptsArgs: true, requireAuth: true, channels: ["slack"],
      async handler(ctx) {
        try {
          allowed(ctx);
          const args = (ctx.args ?? "").trim();
          const [verb = "status", target, option] = args.split(/\s+/);
          if (verb === "status") return statusPayload(await call("status"));
          if (verb === "read" && target) {
            const result = await call("read", { pane: target });
            return { text: `**${target}**\n${code(result.text)}` };
          }
          if (verb === "pending") return { text: code(JSON.stringify(await call("pending"), null, 2)) };
          if (verb === "answer" && /^[a-f0-9]{32}$/.test(target ?? "") && /^\d+$/.test(option ?? "")) {
            allowed(ctx);
            return { text: code(JSON.stringify(await call("answer", { request_id: target, option: Number(option) }))) };
          }
          if (verb === "send" && target) {
            const text = args.replace(/^send\s+\S+\s*/, "");
            allowed(ctx);
            await call("send", { pane: target, text });
            return { text: `Prompt dispatched to ${target}.` };
          }
          return { text: "Use /herdr status | read w4:p2 | pending | send w4:p2 <prompt> | answer <request-id> <zero-based-option>" };
        } catch (error) { return { text: error.message }; }
      },
    });
    api.registerInteractiveHandler({
      channel: "slack", namespace: "herdr-bridge",
      async handler(ctx) {
        try {
          allowed(ctx);
          api.logger.info?.(`Herdr callback received: ${ctx.interaction.payload === "probe" ? "probe" : "approval"}`);
          if (ctx.interaction.payload === "probe") {
            await ctx.respond.editMessage({text: "✅ Herdr Bridge callback connected. No terminal operation was performed.", blocks: []});
            return {handled:true};
          }
          const match = /^([a-f0-9]{32}):(\d+)$/.exec(ctx.interaction.payload);
          if (!match) throw new Error("Invalid Herdr approval callback");
          const result = await call("answer", { request_id: match[1], option: Number(match[2]) });
          api.logger.info?.(`Herdr approval dispatched: ${match[1]} option ${match[2]}`);
          await ctx.respond.editMessage({ text: `Herdr: ${result.option} — dispatched`, blocks: [] });
        } catch (error) {
          api.logger.warn?.(`Herdr callback rejected: ${error.message}`);
          await ctx.respond.reply({ text: error.message });
        }
        return { handled: true };
      },
    });
    // The LLM can inspect terminal state, but cannot synthesize approval provenance.
    for (const [name, method, description, parameters] of [
      ["herdr_status", "status", "List all Herdr agents and exact pane IDs", { type: "object", properties: {}, additionalProperties: false }],
      ["herdr_read", "read", "Read the visible terminal of a specific Herdr pane", { type: "object", properties: { pane: { type: "string" } }, required: ["pane"], additionalProperties: false }],
    ]) {
      api.registerTool({ contextVersion: 2, create(ctx) {
        if (ctx.senderIsOwner !== true) return null;
        return { name, label: name, description, parameters,
          async execute(_id, params) {
            ctx.assertInvocationCurrent();
            if (ctx.senderIsOwner !== true) throw new Error("Herdr tools require the current OpenClaw owner");
            const result = await call(method, params);
            ctx.assertInvocationCurrent();
            return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
          },
        };
      } }, { name, optional: true });
    }
  },
};
