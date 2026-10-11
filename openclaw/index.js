import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { Worker } from "./worker.js";
import { answerText, answerPayload, answerEdit, updateAnswerCard, code, deliver, eventPayload, statusPayload } from "./presentation.js";
import { SlackPaneChannels } from "./channels.js";

export function authorized(config, ctx) {
  return (ctx.isAuthorizedSender ?? ctx.auth?.isAuthorizedSender) === true
    && ctx.channel === config.route.channel
    && ctx.accountId === config.route.accountId
    && config.ownerIds.includes(ctx.senderId);
}

export function registerBridge(api, workerFactory = (...args)=>new Worker(...args), channelFactory = (...args)=>new SlackPaneChannels(...args)) {
    let config = api.pluginConfig;
    let worker, timer, busy = false, stopping = true, controller, pumping, paneChannels, synchronizing;
    const call = (method, params) => {
      if (!worker || stopping) throw new Error("Herdr bridge service is not running");
      return worker.call(method, params);
    };
    const allowed = (ctx) => {
      if (!authorized(config, ctx)) throw new Error("This channel/account/sender is not authorized for Herdr");
      ctx.assertOwnerCurrent?.();
    };
    const sourceRoute = (ctx) => ({...config.route,to:ctx.conversationId?.startsWith("D")
      ? config.route.to : `channel:${ctx.conversationId}`});
    const contextPane = async (ctx) => {
      if (!ctx.conversationId || ctx.conversationId.startsWith("D")) return null;
      const binding = await call("pane_for_channel",{account_id:ctx.accountId,conversation_id:ctx.conversationId});
      if (!binding) throw new Error("This Slack channel is not bound to a Herdr pane");
      return binding;
    };
    const requestAllowed = (ctx, requestId) => call("authorize_request",{
      request_id:requestId,account_id:ctx.accountId,conversation_id:ctx.conversationId});
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
      await ctx.respond.editMessage(await answerEdit(api,sourceRoute(ctx),result));
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
        paneChannels = config.paneChannels?.enabled ? channelFactory(api,config,call) : null;
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
            if (paneChannels && !synchronizing) {
              // Provisioning a new pane must not delay existing channels' events.
              synchronizing = paneChannels.sync(controller.signal)
                .catch(error => { if (!stopping) api.logger.warn(`Herdr pane discovery deferred: ${error.message}`); })
                .finally(() => { synchronizing = undefined; });
            }
            const events = await call("events",paneChannels ? {account_id:config.route.accountId} : undefined);
            let deferred;
            for (let event of events) {
              if (stopping) break;
              try {
                if (["approval.requested","agent.blocked","question.requested"].includes(event.type)) {
                  event = await call("event_for_delivery",{event_id:event.id});
                  if (!event) continue; // Handled locally or deferred: do not deliver/ack.
                }
                let route = config.route;
                if (paneChannels) {
                  const destination = await call("route_event",{event_id:event.id,account_id:config.route.accountId});
                  if (destination) {
                    if (destination.accountId !== config.route.accountId || destination.team_id !== (await paneChannels.identity(controller.signal)).team_id)
                      throw new Error("Event belongs to a different Slack account/workspace");
                    route = destination;
                  } else if (event.type !== "approval.updated") continue;
                }
                if (event.type === "approval.updated") {
                  const requestId = event.result.request_id;
                  const {cards,result} = await call("result_cards", {request_id:requestId});
                  // A text reply has no interactive editor. Its durable follow-up
                  // still needs a portable status message before outbox acknowledgement.
                  if (!cards.length && result.kind === "codex_question")
                    await deliver(api,route,answerPayload(result),controller.signal);
                  let recoverySent = false;
                  for (const binding of cards) {
                    if (stopping) break;
                    await serializeEdit(binding,async()=> {
                      const {result} = await call("result_cards",{request_id:requestId});
                      const edit = cardEditors.get(cardKey(binding));
                      if (!edit && recoverySent) return;
                      await updateAnswerCard(api, route, binding, result, edit, controller.signal);
                      if (!edit) recoverySent = true;
                    });
                  }
                  if (stopping) break;
                } else {
                  // Channel creation can take time. Recheck attention after it, too.
                  if (paneChannels && event.request) {
                    event = await call("event_for_delivery",{event_id:event.id});
                    if (!event) continue;
                  }
                  const payload = eventPayload(event);
                  if (paneChannels && ["approval.requested","agent.blocked","question.requested"].includes(event.type)) {
                    const mention = config.ownerIds.map(id=>`<@${id}>`).join(" ");
                    payload.text = `${mention}\n${payload.text}`;
                    payload.presentation.blocks.unshift({type:"text",text:mention});
                  }
                  await deliver(api, route, payload, controller.signal);
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
        await synchronizing;
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
          const binding = await contextPane(ctx);
          if (verb === "archive" && binding && paneChannels) {
            await paneChannels.archive(binding,controller.signal);
            return {text:"Herdr pane channel archived. History retained."};
          }
          if (binding?.closed) throw new Error("This Herdr pane has closed; its channel is retained as history");
          const pane = (target?.match(/^w\d+:p\d+$/) ? target : null) ?? binding?.pane_id;
          if (binding && pane !== binding.pane_id) throw new Error("Command targets a different pane channel");
          if (verb === "status") return statusPayload((await call("status")).filter(a=>!binding || a.pane_id === binding.pane_id));
          if (verb === "sync" && pane) return {text:code(JSON.stringify(await call("sync_status",{pane}),null,2))};
          if (verb === "bind" && pane) {
            const threadId = target === pane ? option : target;
            if (!threadId) throw new Error("Use /openclaw /herdr bind [pane] <thread-id>");
            return {text:code(JSON.stringify(await call("bind_session",{pane,thread_id:threadId}),null,2))};
          }
          if (verb === "channels") return {text:code(JSON.stringify(await call("pane_channels",{account_id:ctx.accountId}),null,2))};
          if (verb === "read" && pane) {
            const result = await call("read", { pane });
            return { text: `**${pane}**\n${code(result.text)}` };
          }
          if (verb === "pending") return { text: code(JSON.stringify((await call("pending")).filter(r=>!binding || r.pane_id === binding.pane_id), null, 2)) };
          if (verb === "answer" && /^[a-f0-9]{32}$/.test(target ?? "") && /^\d+$/.test(option ?? "")) {
            allowed(ctx);
            await requestAllowed(ctx,target);
            return { text: answerText(await call("answer", { request_id: target, option: Number(option) })) };
          }
          if (verb === "check" && /^[a-f0-9]{32}$/.test(target ?? "")) {
            await requestAllowed(ctx,target);
            return {text:answerText(await call("check",{request_id:target}))};
          }
          if (verb === "reply" && /^[a-f0-9]{32}$/.test(target ?? "")) {
            const text = args.replace(/^reply\s+\S+\s*/, "");
            allowed(ctx);
            await requestAllowed(ctx,target);
            return {text:answerText(await call("answer",{request_id:target,answer_text:text}))};
          }
          if (verb === "send" && pane) {
            const text = args.replace(target === pane ? /^send\s+\S+\s*/ : /^send\s*/, "");
            if (!text.trim()) throw new Error("Prompt text is required");
            allowed(ctx);
            await call("send", { pane, text });
            return { text: `Prompt dispatched to ${pane}.` };
          }
          return { text: "Use /openclaw /herdr status | channels | sync [pane] | bind [pane] <thread-id> | read [pane] | pending | send [pane] <prompt> | answer <request-id> <zero-based-option> | reply <request-id> <text> | check <request-id> | archive (closed pane channel)" };
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
                await ctx.respond.editMessage(await answerEdit(api,sourceRoute(ctx),progress));
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
          // A response-URL reply can replace the source card in this host.
          // Send owner errors separately, preserving its original evidence.
          if (authorized(config,ctx)) {
            try { await deliver(api,sourceRoute(ctx),{text:error.message},controller?.signal); }
            catch (deliveryError) { api.logger.warn?.(`Herdr callback error notification deferred: ${deliveryError.message}`); }
          }
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
