import { test } from "node:test";
import assert from "node:assert/strict";
import plugin, { authorized } from "../index.js";
import { eventPayload, deliver, buttonLabel, messageSummary } from "../presentation.js";
import { readFileSync } from "node:fs";

const config = { route: { channel: "slack", accountId: "default", to: "user:U1" }, ownerIds: ["U1"] };
const trusted = { channel: "slack", accountId: "default", senderId: "U1", isAuthorizedSender: true };
test("command and callback authorization require host authentication plus owner/account/channel", () => {
  assert.ok(authorized(config, trusted));
  assert.ok(authorized(config, { ...trusted, isAuthorizedSender: undefined, auth: { isAuthorizedSender: true } }));
  for (const changes of [{ isAuthorizedSender: false }, { senderId: "U2" }, { accountId: "other" }, { channel: "discord" }])
    assert.equal(authorized(config, { ...trusted, ...changes }), false);
});
test("portable approval actions carry exact request and option, never raw keys", () => {
  const payload = eventPayload({ type: "approval.requested", agent: { pane_id: "w4:p2", kind: "codex" },
    text: "echo hi\n```malicious", request: { id: "a".repeat(32), expires_at: 100,
      options: [{ index: 0, label: "Yes" }] } });
  assert.deepEqual(payload.presentation.blocks.at(-1).buttons[0].action,
    { type: "callback", value: `herdr-bridge:${"a".repeat(32)}:0` });
  assert.ok(!payload.presentation.blocks[1].text.includes("```malicious"));
  assert.ok(!payload.channelData);
  assert.ok(Array.from(buttonLabel({index:1,label:"very long approval ".repeat(20)})).length <= 60);
});
test("delivery uses live OpenClaw config, native normalization/rendering and sends once", async () => {
  const calls = [];
  const cfg = { live: true };
  const adapter = {
    normalizePayload: ({ payload }) => { calls.push("normalize"); return payload; },
    renderPresentation: ({ payload }) => { calls.push("render"); return { ...payload, native: true }; },
    sendPayload: (ctx) => { calls.push("send"); assert.equal(ctx.cfg, cfg); assert.equal(ctx.payload.native, true); return "sent"; },
  };
  const api = { runtime: { config: { current: () => cfg }, channel: { outbound: { loadAdapter: async () => adapter } } } };
  assert.equal(await deliver(api, config.route, { text: "hello", presentation: { blocks: [] } }), "sent");
  assert.deepEqual(calls, ["normalize", "render", "send"]);
  adapter.sendPayload = async () => { calls.push("fail"); throw new Error("offline"); };
  await assert.rejects(deliver(api, config.route, { text: "hello" }), /offline/);
  assert.equal(calls.filter((x) => x === "fail").length, 1);
});
test("registration has no discovery side effects and offers only read tools to models", () => {
  const registrations = {};
  plugin.register({ pluginConfig: config, logger: {}, registerService: (s) => registrations.service = s,
    registerCommand: (c) => registrations.command = c, registerInteractiveHandler: (i) => registrations.interactive = i,
    registerTool: (t, opts) => (registrations.tools ??= []).push({factory:t, name:opts.name}) });
  assert.equal(registrations.service.id, "herdr-bridge");
  assert.equal(registrations.interactive.namespace, "herdr-bridge");
  assert.deepEqual(registrations.tools.map((x) => x.name), ["herdr_status", "herdr_read"]);
  const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url)));
  assert.deepEqual(manifest.contracts.tools, registrations.tools.map((x) => x.name));
  for (const tool of registrations.tools) {
    assert.equal(tool.factory.contextVersion, 2);
    assert.equal(tool.factory.create({senderIsOwner:false}), null);
    assert.equal(tool.factory.create({senderIsOwner:true}).name, tool.name);
  }
});
test("unauthorized callbacks cannot reach the worker or clear a card", async () => {
  let interactive;
  plugin.register({ pluginConfig: config, logger: {}, registerService() {}, registerCommand() {}, registerTool() {},
    registerInteractiveHandler: (i) => interactive = i });
  let text;
  await interactive.handler({ ...trusted, senderId: "U2", interaction: { payload: "a".repeat(32) + ":0" },
    respond: { reply: (r) => text = r.text, editMessage: () => assert.fail("unauthorized edit") } });
  assert.match(text, /not authorized/);
});
test("connection probe is authenticated and never starts a terminal action", async () => {
  let interactive;
  plugin.register({pluginConfig:config,logger:{},registerService(){},registerCommand(){},registerTool(){},
    registerInteractiveHandler:(i)=>interactive=i});
  let response;
  await interactive.handler({...trusted,interaction:{payload:"probe"},respond:{editMessage:(r)=>response=r.text}});
  assert.match(response,/No terminal operation/);
});
test("message heading identifies pane/model/path and summary appears immediately below", () => {
  const payload = eventPayload({type:"approval.requested",agent:{name:"codex-slackbot",pane_id:"w4:p2",model:"gpt-6.1-sol",cwd:"/project"},
    text:"• Old assistant text\nWould you like to run the following command?\n\n  $ uname -s\n\n› 1. Yes, proceed (y)"});
  assert.equal(payload.presentation.title,"codex-slackbot · gpt-6.1-sol · /project");
  assert.equal(payload.presentation.blocks[0].text,"`待审批：uname -s`");
  assert.equal(payload.presentationTextMode,"fallback");
  assert.ok(payload.presentation.blocks[1].text.includes("Old assistant text"));
});
test("completion summary extracts latest assistant paragraph rather than commands or status", () => {
  assert.equal(messageSummary({type:"agent.done",text:"• Ran uname -s\n  └ Darwin\n\n• 已完成新的标题布局，\n  聊天记录继续保留。\n\n› 下一条消息"}),
    "已完成新的标题布局， 聊天记录继续保留。");
});
