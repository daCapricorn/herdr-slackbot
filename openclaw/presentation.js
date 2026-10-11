// Portable presentations only: OpenClaw's channel adapter authors native blocks.
export function literal(text) {
  return String(text ?? "").replace(/[\\`*_\[\]<>]/g, "\\$&");
}
export function code(text) {
  return "```text\n" + String(text ?? "").replaceAll("```", "`\u200b``") + "\n```";
}
export function buttonLabel(option) {
  const label = `${option.index + 1}. ${option.label}`;
  const chars = Array.from(label);
  // Compact summaries keep controls portable; full evidence remains in the body.
  return chars.length > 60 ? chars.slice(0, 59).join("") + "…" : label;
}
function compact(text, max = 200) {
  const chars = Array.from(String(text ?? "").replace(/\s+/g, " ").trim());
  return chars.length > max ? chars.slice(0, max - 1).join("") + "…" : chars.join("");
}
export function messageSummary(event) {
  if (event.summary) return compact(event.summary);
  const text = String(event.text ?? "");
  if (event.type === "approval.requested") {
    const dialog = text.slice(Math.max(0, text.lastIndexOf("Would you like to run the following command?")));
    const command = /(?:^|\n)\s*\$\s+([\s\S]*?)(?=\n\s*(?:[›>]\s*)?1\.\s|$)/m.exec(dialog)?.[1];
    if (command) return compact(`待审批：${command}`);
  }
  const lines = text.split("\n");
  const starts = lines.flatMap((line, i) => /^\s*•\s+/.test(line) && !/^\s*•\s+(?:Ran|Running|Used|Working|Searching|Thinking)\b/.test(line) ? [i] : []);
  if (starts.length) {
    const start = starts.at(-1);
    const paragraph = [lines[start].replace(/^\s*•\s+/, "")];
    for (const line of lines.slice(start + 1)) {
      if (!line.trim() || /^\s*[•›└]/.test(line) || /^\s*(?:Press enter|Would you like|\$)/.test(line)) break;
      paragraph.push(line.trim());
    }
    return compact(paragraph.join(" "));
  }
  return event.type === "agent.done" ? "已完成，请查看下方记录。" : "等待你的处理，请查看下方记录。";
}
export function eventPayload(event) {
  const a = event.agent;
  const title = `${a.name || a.pane_id} · ${a.model || "模型未知"} · ${a.cwd || "目录未知"}`;
  const summary = messageSummary(event);
  const request = event.request;
  const blocks = [
    { type: "text", text: "`" + summary.replaceAll("`", "'") + "`" },
    { type: "text", text: code(event.text) },
  ];
  if (event.type === "question.requested") {
    blocks.splice(1,0,{type:"text",text:request.question.title});
    if (request.options.length) blocks.splice(2,0,{type:"text",text:request.options.map(o=>`${o.index + 1}. ${o.label}`).join("\n")});
    blocks.push({type:"context",text:`可点选项，或发送 /herdr reply ${request.id} <你的回答>。问题会在当前 Codex 回合结束时失效。`});
  }
  if (request?.options?.length) {
    blocks.push({ type: "context", text: `Request ${request.id} · expires ${new Date(request.expires_at * 1000).toISOString()}` });
    blocks.push({ type: "buttons", buttons: request.options.map((o) => ({
      label: buttonLabel(o),
      action: { type: "callback", value: `herdr-bridge:${request.id}:${o.index}` },
    })) });
  } else if (event.type === "agent.blocked") {
    blocks.push({ type: "context", text: "This dialog requires a local answer. Remote keys are disabled for unrecognized, multi-select, typed, or file-backed plan dialogs." });
  }
  return { text: `${title}\n${summary}`, presentationTextMode: "fallback", presentation: {
    title, tone: event.type === "agent.done" ? "success" : "warning", blocks,
  } };
}
export function statusPayload(agents) {
  return { text: agents.length ? agents.map((a) =>
    `**${literal(a.pane_id)}** · ${literal(a.kind)} · **${literal(a.status)}**\n${literal(a.title || a.cwd || "")}`
  ).join("\n\n") : "No Herdr agents are running." };
}

export async function deliver(api, route, payload, signal) {
  const adapter = await api.runtime.channel.outbound.loadAdapter(route.channel);
  if (!adapter?.sendPayload) throw new Error(`OpenClaw channel ${route.channel} has no payload sender`);
  const cfg = api.runtime.config.current();
  const ctx = { cfg, to: route.to, accountId: route.accountId, text: payload.text ?? "", payload, signal,
    assertDirectAdapterHandoff: () => signal?.throwIfAborted() };
  const normalized = adapter.normalizePayload ? adapter.normalizePayload({ cfg, accountId: route.accountId, payload }) : payload;
  if (!normalized) throw new Error("OpenClaw channel suppressed the bridge payload");
  ctx.payload = normalized;
  if (normalized.presentation && adapter.renderPresentation) {
    ctx.payload = await adapter.renderPresentation({ payload: normalized, presentation: normalized.presentation, ctx }) ?? normalized;
  }
  signal?.throwIfAborted();
  return adapter.sendPayload(ctx);
}
export function answerText(result) {
  const option = String(result.option ?? "Selected option");
  if (result.kind === "codex_question") {
    if (result.outcome === "confirmed") return `✅ Herdr: ${option} — confirmed. Codex recorded the answer to this question.`;
    if (["checking","dispatching"].includes(result.outcome)) return `⏳ Herdr: ${option} — checking question acceptance. Repeated clicks do not resend the answer.`;
    if (result.outcome === "not_sent") return `⚠️ Herdr: ${option} — this attempt has not submitted an answer. Use Check status before submitting again.`;
    if (result.outcome === "unconfirmed") return `⚠️ Herdr: ${option} — unconfirmed. The question is still pending. No answer was retried automatically.${result.watching ? " Background checks continue." : " Use Check status to inspect again."}`;
    return `⚠️ Herdr: ${option} — question acceptance is unknown (${result.verification?.reason ?? "not_observed"}). No answer was retried automatically.`;
  }
  if (result.outcome === "confirmed")
    return `✅ Herdr: ${option} — confirmed. The original approval has closed; command completion is not verified.`;
  if (["checking", "dispatching"].includes(result.outcome))
    return `⏳ Herdr: ${option} — checking terminal acceptance. Repeated clicks do not resend keys.`;
  if (result.outcome === "not_sent")
    return `⚠️ Herdr: ${option} — this attempt has not sent keys. Use Check status, then explicitly submit again.`;
  if (result.outcome === "unconfirmed")
    return `⚠️ Herdr: ${option} — unconfirmed. The terminal still shows this approval. No keys were retried automatically.${result.watching ? " Background checks continue." : " Use Check status to inspect again."}`;
  return `⚠️ Herdr: ${option} — terminal acceptance is unknown. No keys were retried automatically.${result.watching ? " Background checks continue." : " Use Check status to inspect again."}`;
}

export function answerPayload(result) {
  const text = answerText(result);
  const blocks = [{type:"text",text}];
  if (result.outcome !== "confirmed") {
    const buttons = [{label:"检查状态",action:{type:"callback",value:`herdr-bridge:check:${result.request_id}`}}];
    if (result.retry_token) {
      blocks.push({type:"context",text:result.kind === "codex_question"
        ? "重新提交会再次发送同一回答；之前的输入仍可能延迟生效。"
        : "重新提交会再次发送同一选项；之前的按键仍可能延迟生效。"});
      buttons.push({label:"重新提交",action:{type:"callback",value:`herdr-bridge:retry:${result.request_id}:${result.retry_token}`}});
    }
    blocks.push({type:"buttons",buttons});
  }
  return {text,presentationTextMode:"fallback",presentation:{blocks}};
}

// OpenClaw 2026.9.9 renders native Slack edit blocks as presentation segments.
export async function answerEdit(api, route, result) {
  if (result.outcome === "confirmed") return {text:answerText(result),blocks:[]};
  const adapter = await api.runtime.channel.outbound.loadAdapter(route.channel);
  const cfg = api.runtime.config.current();
  const payload = answerPayload(result);
  const ctx = {cfg,to:route.to,accountId:route.accountId,text:payload.text,payload};
  const rendered = await adapter?.renderPresentation?.({payload,presentation:payload.presentation,ctx});
  const segments = rendered?.channelData?.slack?.renderedPresentationSegments;
  if (!Array.isArray(segments) || segments.some((s)=>s.kind !== "blocks" || !Array.isArray(s.blocks)))
    throw new Error("OpenClaw Slack renderer did not provide native recovery controls");
  return {text:payload.text,blocks:segments.flatMap((s)=>s.blocks)};
}

export async function updateAnswerCard(api, route, binding, result, edit, signal) {
  if (binding.channel !== route.channel || binding.accountId !== route.accountId
      || !/^D[A-Z0-9]+$/.test(binding.conversationId) || !/^\d+\.\d+$/.test(binding.messageTs))
    throw new Error("Approval card binding does not match this Slack DM route");
  if (edit) {
    await edit(await answerEdit(api, route, result));
    return;
  }
  // The public SDK has no general background edit API. After a restart, use
  // OpenClaw's portable sender for a new recovery card instead of private clients.
  const payload = answerPayload(result);
  payload.presentation.blocks.splice(1,0,{type:"context",text:`Request ${result.request_id} · 恢复状态`});
  await deliver(api,route,payload,signal);
}
