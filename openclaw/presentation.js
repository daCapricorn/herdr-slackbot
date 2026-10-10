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
  if (result.outcome === "confirmed")
    return `✅ Herdr: ${option} — confirmed. The original approval has closed; command completion is not verified.`;
  if (result.outcome === "unconfirmed")
    return `⚠️ Herdr: ${option} — unconfirmed. The terminal still shows this approval. No keys were retried; check it locally.`;
  return `⚠️ Herdr: ${option} — terminal acceptance is unknown. No keys were retried; check it locally.`;
}
