import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { Worker } from "./worker.js";
import { answerText, answerPayload, answerEdit, updateAnswerCard, code, deliver, eventPayload, statusPayload } from "./presentation.js";
import { SlackPaneChannels } from "./channels.js";

function slackUserId(value) {
  return typeof value === "string" && /^[UW][A-Z0-9]+$/i.test(value) ? value.toUpperCase() : value;
}

export function commandContext(ctx) {
  // OpenClaw native commands expose the authenticated origin in `from`.
  // `channelId` is the provider name (slack), not the Slack conversation id.
  const origin = /^slack:(?:(?:channel|group):)?([CDG][A-Z0-9]+)$/i.exec(ctx.from ?? "")?.[1]?.toUpperCase();
  if (origin && ctx.conversationId && origin !== ctx.conversationId)
    throw new Error("Conflicting Slack command conversation identity");
  // Never infer a channel from user-authored args or from slash:<user-id>.
  return {...ctx, senderId:slackUserId(ctx.senderId), conversationId:origin ?? ctx.conversationId};
}

export function authorized(config, ctx) {
  return (ctx.isAuthorizedSender ?? ctx.auth?.isAuthorizedSender) === true
    && ctx.channel === config.route.channel
    && ctx.accountId === config.route.accountId
    && config.ownerIds.map(slackUserId).includes(slackUserId(ctx.senderId));
}

export function stripBotMention(text, botId, mentionStripPatterns = []) {
  if (typeof text !== "string" || typeof botId !== "string") return "";
  const literal = `<@${botId}>`;
  const rendered = mentionStripPatterns.find(pattern => typeof pattern === "string"
    && (pattern.toUpperCase() === literal.toUpperCase()
      || pattern.toUpperCase().startsWith(`${literal.toUpperCase()} (`) && pattern.endsWith(")")));
  const exact = rendered ?? literal;
  const escaped = exact.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
  return text.replace(new RegExp(escaped,"gi"),"").trim()
    .replace(/&(amp|lt|gt);/g, entity => ({"&amp;":"&","&lt;":"<","&gt;":">"})[entity]);
}

function inputStatus(result) {
  if (result.outcome === "confirmed") return `Prompt recorded by ${result.kind} in ${result.pane_id}.`;
  if (result.outcome === "queued") return `Prompt queued for ${result.pane_id}; it will be rechecked when the agent is idle.`;
  if (result.outcome === "rejected") return result.reason === "agent_blocked_use_approval"
    ? "Prompt was not sent. Use the exact approval or question controls already shown for this pane."
    : "Prompt was not sent because the pane, session, or composer could not be verified safe.";
  return `Prompt submission to ${result.pane_id ?? "the pane"} is uncertain because no exact transcript receipt was observed. It was not retried.`;
}

function finishTakeover(hookCtx, {outcome="skipped", reason, text} = {}) {
  let queuedFinal = false;
  try {
    if (text && !hookCtx.abortSignal?.aborted)
      queuedFinal = Boolean(hookCtx.dispatcher.sendFinalReply({text}));
  } finally {
    hookCtx.dispatcher.markComplete();
    hookCtx.recordProcessed(outcome,{reason});
    hookCtx.markIdle("message_completed");
  }
  return {handled:true,queuedFinal,counts:hookCtx.dispatcher.getQueuedCounts()};
}

export function registerBridge(api, workerFactory = (...args)=>new Worker(...args), channelFactory = (...args)=>new SlackPaneChannels(...args)) {
    let config = api.pluginConfig;
    let worker, timer, busy = false, stopping = true, controller, pumping, paneChannels, synchronizing;
    const knownChannels = new Map();
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
    const channelKey = (accountId,conversationId) => `${accountId}:${conversationId}`;
    const rememberBindings = (bindings) => {
      for (const binding of bindings ?? [])
        if (binding?.account_id && binding?.channel_id)
          knownChannels.set(channelKey(binding.account_id,binding.channel_id),binding);
      return bindings;
    };
    const refreshKnownChannels = async()=>rememberBindings(await call("pane_channels",{account_id:config.route.accountId}));
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
        try { await refreshKnownChannels(); }
        catch (error) { api.logger.warn(`Herdr pane input cache unavailable: ${error?.name ?? "Error"}`); }
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
              synchronizing = paneChannels.sync(controller.signal).then(rememberBindings).then(refreshKnownChannels)
                .catch(error => { if (!stopping) api.logger.warn(`Herdr pane discovery deferred: ${error.message}`); })
                .finally(() => { synchronizing = undefined; });
            }
            const events = await call("events",paneChannels ? {account_id:config.route.accountId} : undefined);
            let deferred;
            const failedConversations = new Set();
            for (let event of events) {
              if (stopping) break;
              const conversation = event.type === "conversation.message"
                ? (event.agent.terminal_id ?? event.agent.pane_id) : null;
              // Retain chronological order within a pane after a failed send;
              // other panes and independent completion/results can still progress.
              if (conversation && failedConversations.has(conversation)) continue;
              try {
                if (event.type === "input.updated") {
                  const route = event.route;
                  const identity = await paneChannels?.identity(controller.signal);
                  if (!route || route.channel !== "slack" || route.accountId !== config.route.accountId
                      || !/^[CG][A-Z0-9]+$/.test(route.conversationId)
                      || route.to !== `channel:${route.conversationId}`
                      || !identity?.team_id || route.team_id !== identity.team_id)
                    throw new Error("Queued input update route is no longer authorized");
                  await deliver(api,route,{text:inputStatus(event.result)},controller.signal);
                  await call("ack", {event_id:event.id});
                  continue;
                }
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
                if (conversation) failedConversations.add(conversation);
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
    if (typeof api.on === "function") api.on("reply_dispatch", async(event, hookCtx) => {
      const ctx = event?.ctx ?? {};
      const surface = String(ctx.Surface ?? ctx.Provider ?? ctx.OriginatingChannel ?? "").toLowerCase();
      const accountId = ctx.AccountId, conversationId = ctx.NativeChannelId;
      if (surface !== "slack" || accountId !== config.route.accountId
          || typeof conversationId !== "string" || conversationId.startsWith("D")) return;
      const key = channelKey(accountId,conversationId);
      let binding;
      try {
        binding = await call("pane_for_channel",{account_id:accountId,conversation_id:conversationId});
        if (!binding) return;
        knownChannels.set(key,binding);
      } catch (error) {
        if (!knownChannels.has(key)) return;
        api.logger.warn(`Herdr pane input lookup failed closed: ${error?.name ?? "Error"}`);
        return finishTakeover(hookCtx,{outcome:"error",reason:"herdr_lookup_failed",
          text:"Herdr could not verify this pane binding, so no prompt was sent."});
      }
      const failClosed = (reason,text)=>finishTakeover(hookCtx,{outcome:"skipped",reason,text});
      try {
        if (event.sendPolicy !== "allow" || hookCtx.abortSignal?.aborted)
          return failClosed("herdr_dispatch_not_authorized");
        if (ctx.InboundEventKind && ctx.InboundEventKind !== "user_request")
          return failClosed("herdr_non_message_update");
        if (ctx.ProviderUpdateKind && ctx.ProviderUpdateKind !== "message")
          return failClosed("herdr_non_message_update");
        if (ctx.SenderIsBot === true) return failClosed("herdr_bot_sender");
        const senderId = slackUserId(ctx.SenderId);
        if (ctx.CommandAuthorized !== true || !config.ownerIds.map(slackUserId).includes(senderId))
          return failClosed("herdr_sender_not_owner");
        const identity = await paneChannels?.identity(controller?.signal);
        if (!identity?.user_id || !identity?.team_id || binding.team_id !== identity.team_id
            || ctx.GroupSpace !== identity.team_id)
          return failClosed("herdr_workspace_identity_mismatch",
            "Herdr could not verify this pane's Slack workspace, so no prompt was sent.");
        const mentioned = Array.isArray(ctx.MentionedUserIds) && ctx.MentionedUserIds.map(slackUserId).includes(slackUserId(identity.user_id));
        if (!mentioned) return failClosed("herdr_unmentioned");
        const raw = ctx.ChannelContext?.chat?.commandSourceText;
        if (typeof raw !== "string" || !new RegExp(`<@${identity.user_id}>`,"i").test(raw))
          return failClosed("herdr_raw_mention_unavailable",
            "Herdr could not verify the explicit OpenClaw mention, so no prompt was sent.");
        const text = stripBotMention(raw,identity.user_id,ctx.ChannelContext?.chat?.mentionStripPatterns);
        if (!text) return failClosed("herdr_empty_prompt","Add prompt text after mentioning OpenClaw.");
        if (!ctx.MessageSid || hookCtx.abortSignal?.aborted)
          return failClosed("herdr_message_identity_unavailable",
            "Herdr could not verify this Slack message identity, so no prompt was sent.");
        const result = await call("ingest_input",{account_id:accountId,conversation_id:conversationId,
          message_ts:String(ctx.MessageSid),sender_id:senderId,text,
          thread_id:ctx.MessageThreadId == null ? null : String(ctx.MessageThreadId),binding_id:binding.id});
        return finishTakeover(hookCtx,{outcome:"completed",reason:`herdr_${result.outcome}`,text:inputStatus(result)});
      } catch (error) {
        api.logger.warn(`Herdr pane input failed closed: ${error?.name ?? "Error"}`);
        return finishTakeover(hookCtx,{outcome:"error",reason:"herdr_input_failed",
          text:"Herdr could not safely submit this prompt. No automatic retry will occur."});
      }
    });
    api.registerCommand({
      name: "herdr", description: "Herdr status, read, pending, send, approvals, and question replies",
      acceptsArgs: true, requireAuth: true, channels: ["slack"],
      async handler(ctx) {
        try {
          ctx = commandContext(ctx);
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
