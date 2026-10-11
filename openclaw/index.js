import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { Worker } from "./worker.js";
import { answerText, answerPayload, answerEdit, updateAnswerCard, code, deliver, eventPayload, statusPayload } from "./presentation.js";

export function authorized(config, ctx) {
  return (ctx.isAuthorizedSender ?? ctx.auth?.isAuthorizedSender) === true
    && ctx.channel === config.route.channel
    && ctx.accountId === config.route.accountId
    && config.ownerIds.includes(ctx.senderId);
}

export function registerBridge(api, workerFactory = (...args)=>new Worker(...args)) {
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
    const edits = new Map();
    // Authenticated callback editors stay in memory; never persist response URLs.
    const cardEditors = new Map();
    const cardKey = (binding) => `${binding.accountId}:${binding.conversationId}:${binding.messageTs}`;
    const serializeEdit = async (binding, edit) => {
      const key = cardKey(binding);
      const prior = edits.get(key) ?? Promise.resolve();
      const next = prior.catch(()=>{}).then(edit);
      edits.set(key,next);
      try { return await next; }
      finally { if (edits.get(key) === next) edits.delete(key); }
    };
    const editCurrentCard = (ctx, requestId) => serializeEdit({accountId:ctx.accountId,
      conversationId:ctx.conversationId,messageTs:ctx.interaction.messageTs}, async()=> {
      const {result} = await call("result_cards",{request_id:requestId});
      await ctx.respond.editMessage(await answerEdit(api,config.route,result));
    });
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
          worker = workerFactory(config, join(stateDir, "bridge.sqlite"), api.logger);
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
            let deferred;
            for (const event of events) {
              if (stopping) break;
              try {
                if (event.type === "approval.updated") {
                  const requestId = event.result.request_id;
                  const {cards,result} = await call("result_cards", {request_id:requestId});
                  // A text reply has no interactive editor. Its durable follow-up
                  // still needs a portable status message before outbox acknowledgement.
                  if (!cards.length && result.kind === "codex_question")
                    await deliver(api,config.route,answerPayload(result),controller.signal);
                  let recoverySent = false;
                  for (const binding of cards) {
                    if (stopping) break;
                    await serializeEdit(binding,async()=> {
                      const {result} = await call("result_cards",{request_id:requestId});
                      const edit = cardEditors.get(cardKey(binding));
                      if (!edit && recoverySent) return;
                      await updateAnswerCard(api, config.route, binding, result, edit, controller.signal);
                      if (!edit) recoverySent = true;
                    });
                  }
                  if (stopping) break;
                } else {
                  await deliver(api, config.route, eventPayload(event), controller.signal);
                }
                await call("ack", { event_id: event.id });
                api.logger.info(`Herdr event delivered: ${event.type} ${event.id}`);
              } catch (error) {
                deferred ??= error;
                api.logger.warn(`Herdr event deferred: ${event.id} ${error.message}`);
              }
            }
            if (deferred) throw deferred;

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
        await Promise.allSettled([...edits.values()]);
        cardEditors.clear();
        await worker?.stop();
        worker = undefined;
      },
    });
    api.registerCommand({
      name: "herdr", description: "Herdr status, read, pending, send, approvals, and question replies",
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
            return { text: answerText(await call("answer", { request_id: target, option: Number(option) })) };
          }
          if (verb === "check" && /^[a-f0-9]{32}$/.test(target ?? ""))
            return {text:answerText(await call("check",{request_id:target}))};
          if (verb === "reply" && /^[a-f0-9]{32}$/.test(target ?? "")) {
            const text = args.replace(/^reply\s+\S+\s*/, "");
            allowed(ctx);
            return {text:answerText(await call("answer",{request_id:target,answer_text:text}))};
          }
          if (verb === "send" && target) {
            const text = args.replace(/^send\s+\S+\s*/, "");
            allowed(ctx);
            await call("send", { pane: target, text });
            return { text: `Prompt dispatched to ${target}.` };
          }
          return { text: "Use /herdr status | read w4:p2 | pending | send w4:p2 <prompt> | answer <request-id> <zero-based-option> | reply <request-id> <text> | check <request-id>" };
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
          const payload = ctx.interaction.payload;
          const answer = /^([a-f0-9]{32}):(\d+)$/.exec(payload);
          const check = /^check:([a-f0-9]{32})$/.exec(payload);
          const retry = /^retry:([a-f0-9]{32}):([a-f0-9]{32})$/.exec(payload);
          const requestId = (answer ?? check ?? retry)?.[1];
          if (!requestId) throw new Error("Invalid Herdr approval callback");
          cardEditors.set(cardKey({accountId:ctx.accountId,conversationId:ctx.conversationId,
            messageTs:ctx.interaction.messageTs}),message=>ctx.respond.editMessage(message));
          if (cardEditors.size > 1024) cardEditors.delete(cardEditors.keys().next().value);
          // Persist the authenticated source card before any terminal write.
          await call("bind_card", {request_id:requestId,binding:{channel:ctx.channel,accountId:ctx.accountId,
            conversationId:ctx.conversationId,messageTs:ctx.interaction.messageTs}});
          let result;
          if (answer || retry) {
            const prepared = await call("prepare_answer", {request_id:requestId,
              ...(answer ? {option:Number(answer[2])} : {retry_from:retry[2]})});
            if (prepared.ready_to_dispatch) {
              // No terminal write occurs before this progress card is shown.
              await serializeEdit({accountId:ctx.accountId,conversationId:ctx.conversationId,
                messageTs:ctx.interaction.messageTs},async()=> {
                const {result:latest} = await call("result_cards",{request_id:requestId});
                const progress = latest.outcome === "not_sent" && latest.attempt_id === prepared.attempt_id
                  ? {...latest,outcome:"checking",retry_token:null} : latest;
                await ctx.respond.editMessage(await answerEdit(api,config.route,progress));
              });
              allowed(ctx);
              result = await call("dispatch_reserved",{request_id:requestId,attempt_id:prepared.attempt_id});
            } else if (prepared.outcome === "checking" || prepared.outcome === "confirmed") {
              result = prepared;
            } else {
              result = await call("check",{request_id:requestId});
            }
          } else {
            result = await call("check", {request_id:requestId});
          }
          api.logger.info?.(`Herdr approval ${result.outcome}: ${requestId} reason=${result.verification?.reason ?? "unknown"}`);
          // Background edits use latest persisted results. The per-card queue also
          // prevents a slow earlier callback from overwriting a later confirmation.
          await editCurrentCard(ctx, requestId);
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
}

export default {
  id: "herdr-bridge", name: "Herdr Bridge", version: "0.2.0",
  register: registerBridge,
};
