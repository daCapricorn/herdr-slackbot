import { test } from "node:test";
import assert from "node:assert/strict";
import plugin, { authorized } from "../index.js";
import { answerText, eventPayload, deliver, buttonLabel, messageSummary } from "../presentation.js";
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
  assert.ok(!JSON.stringify(payload.presentation).includes("expires"));
  assert.ok(Array.from(buttonLabel({index:1,label:"very long approval ".repeat(20)})).length <= 60);
});
test("pane conversation events keep authored Markdown and do not ping Slack users", () => {
  const payload = eventPayload({type:"conversation.message",role:"assistant",text:"Ran: `pytest -q`\n\nUse **bold** <@U123> <!here>",
    agent:{pane_id:"w4:p2",name:"codex-slackbot"}});
  assert.equal(payload.presentation.blocks[0].text,"🤖 codex-slackbot");
  assert.match(payload.presentation.blocks[1].text,/\*\*bold\*\*/);
  assert.doesNotMatch(payload.presentation.blocks[1].text,/(?<!&lt;)<@U123>/);
  assert.doesNotMatch(payload.presentation.blocks[1].text,/(?<!&lt;)<!here>/);
  assert.match(payload.text,/Ran: `pytest -q`/);
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
  await interactive.handler({ ...trusted, senderId: "U2", interaction: { payload: "a".repeat(32) + ":0" },
    respond: { reply: () => assert.fail("unauthorized response-URL replacement"), editMessage: () => assert.fail("unauthorized edit") } });
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

test("an acknowledged key write never becomes a success card without terminal confirmation", () => {
  const dropped = answerText({option:"Yes, proceed",outcome:"unconfirmed"});
  assert.match(dropped, /unconfirmed/);
  assert.match(dropped, /still shows this approval/);
  assert.match(dropped, /No keys were retried/);
  assert.ok(!dropped.includes("✅"));
  for (const outcome of ["uncertain", "dispatched", undefined]) {
    const unknown = answerText({option:"Yes, proceed",outcome});
    assert.match(unknown, /acceptance is unknown/);
    assert.ok(!unknown.includes("✅"));
  }
  assert.match(answerText({option:"Yes, proceed",outcome:"confirmed"}), /✅.*original approval has closed/);
});

test("unconfirmed cards expose separate read-only check and attempt-bound retry controls", async () => {
  const {answerPayload, answerEdit} = await import("../presentation.js");
  const id = "a".repeat(32), attempt = "b".repeat(32);
  const payload = answerPayload({request_id:id,outcome:"unconfirmed",retry_token:attempt});
  const actions = payload.presentation.blocks.at(-1).buttons.map(b=>b.action.value);
  assert.deepEqual(actions,[`herdr-bridge:check:${id}`,`herdr-bridge:retry:${id}:${attempt}`]);
  const unknown = answerPayload({request_id:id,outcome:"uncertain"});
  assert.equal(unknown.presentation.blocks.at(-1).buttons.length,1);
  const success = answerPayload({request_id:id,outcome:"confirmed"});
  assert.ok(!success.presentation.blocks.some(b=>b.type === "buttons"));
  const api = {runtime:{config:{current:()=>({})},channel:{outbound:{loadAdapter:async()=>({
    renderPresentation:({payload})=>({channelData:{slack:{renderedPresentationSegments:[{kind:"blocks",blocks:payload.presentation.blocks}]}}})
  })}}}};
  assert.deepEqual((await answerEdit(api,config.route,{request_id:id,outcome:"uncertain"})).blocks,unknown.presentation.blocks);
  api.runtime.channel.outbound.loadAdapter = async()=>({renderPresentation:()=>null});
  await assert.rejects(answerEdit(api,config.route,{request_id:id,outcome:"uncertain"}),/native recovery controls/);
});

test("checking and confirmed edits retain original context while replacing choice buttons",async()=> {
  const {answerPayload,answerEdit}=await import("../presentation.js");
  const id="a".repeat(32);
  const card_context={type:"approval.requested",agent:{name:"pane-name",model:"model",cwd:"/project"},
    text:"original transcript\n$ uname -s",summary:"original summary",
    request:{id,expires_at:200,options:[{index:0,label:"Yes"}]}};
  const api={runtime:{config:{current:()=>({})},channel:{outbound:{loadAdapter:async()=>({
    renderPresentation:({payload})=>({channelData:{slack:{renderedPresentationSegments:[{kind:"blocks",blocks:payload.presentation.blocks}]}}})
  })}}}};
  for(const type of ["approval.requested","question.requested"])
  for(const outcome of ["checking","unconfirmed","confirmed"]) {
    const context={...card_context,type,request:{...card_context.request,question:{title:"Original question"}}};
    const result={request_id:id,outcome,option:"Yes",card_context:context};
    const payload=answerPayload(result);
    assert.equal(payload.text,"");
    assert.equal(payload.presentationTextMode,undefined);
    assert.equal(payload.presentation.title,"pane-name · model · /project");
    assert.equal(payload.presentation.blocks[0].text,"`original summary`");
    assert.ok(payload.presentation.blocks.some(b=>b.text?.includes("original transcript")));
    const buttons=payload.presentation.blocks.filter(b=>b.type === "buttons").flatMap(b=>b.buttons);
    assert.ok(!buttons.some(b=>b.action.value === `herdr-bridge:${id}:0`));
    assert.equal(buttons.length,outcome === "confirmed" ? 0 : 1);
    const edited=await answerEdit(api,config.route,result);
    assert.deepEqual(edited.blocks,payload.presentation.blocks);
  }
});

async function runningBridge(t, initial = {}) {
  const {registerBridge} = await import("../index.js");
  const {mkdtemp, rm} = await import("node:fs/promises");
  const {tmpdir} = await import("node:os");
  const dir = await mkdtemp(`${tmpdir()}/herdr-plugin-test-`);
  const id = "a".repeat(32), attempt = "b".repeat(32);
  const binding = {channel:"slack",accountId:"default",conversationId:"D123",messageTs:"123.456"};
  const registrations = {}, calls = [], edits = [];
  let result = {request_id:id,outcome:"unconfirmed",attempt_id:attempt,retry_token:attempt,option:"Yes",...initial.result};
  const worker = {child:{exitCode:null},start(){},async stop(){}, async call(method,params) {
    calls.push({method,params});
    if (initial.call) return initial.call(method,params,result);
    if (method === "events") return initial.events ?? [];
    if (method === "event_for_delivery") return initial.eventForDelivery === null ? null
      : (initial.eventForDelivery ?? initial.events?.find(e=>e.id === params.event_id));
    if (method === "result_cards") return {result,cards:initial.cards ?? [binding]};
    if (method === "prepare_answer") {
      if (result.outcome === "confirmed") return {...result,ready_to_dispatch:false};
      result = {...result,outcome:"not_sent",retry_token:null};
      return {...result,outcome:"checking",ready_to_dispatch:true};
    }
    if (method === "dispatch_reserved") {
      result = {...result,outcome:"unconfirmed",retry_token:attempt};
      return result;
    }
    if (method === "check") return result;
    return {};
  }};
  const context = {send:initial.update ?? (async(message)=>edits.push(message))};
  const api = {pluginConfig:initial.config ?? config,logger:{info(){},warn(){}},
    registerService:s=>registrations.service=s,registerCommand:c=>registrations.command=c,
    registerInteractiveHandler:h=>registrations.interactive=h,registerTool(){},
    runtime:{config:{current:()=>({})},channel:{
      outbound:{loadAdapter:async()=>({sendPayload:ctx=>context.send(ctx),renderPresentation:({payload})=>({...payload,channelData:{slack:{
        renderedPresentationSegments:[{kind:"blocks",blocks:payload.presentation.blocks}]
      }}})})}
    }}
  };
  registerBridge(api,()=>worker,initial.channelFactory);
  await registrations.service.start({stateDir:dir,config:{}});
  t.after(async()=> {await registrations.service.stop();await rm(dir,{recursive:true,force:true});});
  const click = async(payload)=>registrations.interactive.handler({...trusted,conversationId:binding.conversationId,
    interaction:{payload,messageTs:binding.messageTs},respond:{
      editMessage:async(message)=> {initial.onEdit?.(message,calls);if (initial.editFail) throw new Error("edit offline");edits.push(message);},
      reply:async(message)=>edits.push({error:message.text})
    }});
  return {id,attempt,binding,calls,edits,api,context,click,registrations,setResult:r=>result=r};
}

test("authorized answer/check/retry callbacks bind the source card and invoke only their own method", async t => {
  const bridge = await runningBridge(t);
  for (const [payload,method,params] of [
    [`${bridge.id}:0`,"prepare_answer",{request_id:bridge.id,option:0}],
    [`check:${bridge.id}`,"check",{request_id:bridge.id}],
    [`retry:${bridge.id}:${bridge.attempt}`,"prepare_answer",{request_id:bridge.id,retry_from:bridge.attempt}],
  ]) {
    bridge.calls.length = 0;
    await bridge.click(payload);
    const bound = bridge.calls.findIndex(c=>c.method === "bind_card");
    const invoked = bridge.calls.findIndex(c=>c.method === method);
    assert.ok(bound >= 0 && invoked > bound);
    assert.deepEqual(bridge.calls[invoked].params,params);
    assert.deepEqual(bridge.calls[bound].params.binding,bridge.binding);
    assert.equal(bridge.calls.filter(c=>["prepare_answer","check"].includes(c.method)).length,1);
    assert.equal(bridge.calls.filter(c=>c.method === "dispatch_reserved").length,method === "check" ? 0 : 1);
    assert.ok(bridge.edits.at(-1).blocks.at(-1).buttons);
  }
  const before = bridge.calls.length;
  await bridge.click(`retry:${bridge.id}:arbitrary-keys`);
  assert.equal(bridge.calls.length,before);
  assert.match(bridge.edits.at(-1).payload.text,/Invalid/);
});

test("callbacks edit the latest stored result rather than their potentially stale return", async t => {
  const bridge = await runningBridge(t);
  bridge.setResult({request_id:bridge.id,outcome:"confirmed",option:"Yes"});
  await bridge.click(`check:${bridge.id}`);
  assert.match(bridge.edits.at(-1).text,/confirmed/);
  assert.equal(bridge.edits.at(-1).blocks.length,0);
});

test("after restart persisted updates send a portable recovery card before acknowledging the outbox", async t => {
  const bridge = await runningBridge(t,{events:[{id:"event-1",type:"approval.updated",result:{request_id:"a".repeat(32)}}]});
  // The initial service pump is asynchronous; allow only bounded scheduling turns.
  for (let i=0; i<20 && !bridge.calls.some(c=>c.method === "ack");i++)
    await new Promise(resolve=>setImmediate(resolve));
  assert.equal(bridge.edits.length,1);
  assert.equal(bridge.edits[0].to,config.route.to);
  assert.match(bridge.edits[0].payload.presentation.blocks[1].text,/恢复状态/);
  assert.equal(bridge.edits[0].payload.presentation.blocks.at(-1).buttons[0].action.value,`herdr-bridge:check:${bridge.id}`);
  assert.deepEqual(bridge.calls.at(-1),{method:"ack",params:{event_id:"event-1"}});
});

test("background recovery fails closed on account or DM mismatch", async t => {
  const {updateAnswerCard} = await import("../presentation.js");
  const bridge = await runningBridge(t);
  const result = {request_id:bridge.id,outcome:"uncertain"};
  await assert.rejects(updateAnswerCard(bridge.api,config.route,{...bridge.binding,accountId:"other"},result),/does not match/);
  await assert.rejects(updateAnswerCard(bridge.api,config.route,{...bridge.binding,conversationId:"C123"},result),/does not match/);
  assert.equal(bridge.edits.length,0);
});

test("a live authenticated editor updates the original card without a native approval context or new delivery", async t => {
  const {updateAnswerCard} = await import("../presentation.js");
  const bridge = await runningBridge(t);
  const edits = [];
  await updateAnswerCard(bridge.api,config.route,bridge.binding,
    {request_id:bridge.id,outcome:"confirmed"},async message=>edits.push(message));
  assert.equal(bridge.edits.length,0);
  assert.match(edits[0].text,/confirmed/);
  assert.deepEqual(edits[0].blocks,[]);
});

test("restart recovery sends only one replacement for duplicate bound cards", async t => {
  const bridge = await runningBridge(t,{events:[{id:"duplicate",type:"approval.updated",result:{request_id:"a".repeat(32)}}],cards:[
    {channel:"slack",accountId:"default",conversationId:"D123",messageTs:"123.456"},
    {channel:"slack",accountId:"default",conversationId:"D123",messageTs:"123.789"},
  ]});
  for(let i=0;i<20 && !bridge.calls.some(c=>c.method === "ack");i++)
    await new Promise(resolve=>setImmediate(resolve));
  assert.equal(bridge.edits.length,1);
});

test("failed Slack edits leave background updates unacknowledged for later retry", async t => {
  const bridge = await runningBridge(t,{events:[{id:"event-2",type:"approval.updated",result:{request_id:"a".repeat(32)}}]});
  bridge.context.send = async()=> {throw new Error("Slack offline");};
  for (let i=0;i<20;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.ok(!bridge.calls.some(c=>c.method === "ack"));
});

test("progress is displayed before dispatch and contains no original option/retry buttons", async t => {
  let first = true;
  const bridge = await runningBridge(t,{onEdit:(message,calls)=> {
    if (!first) return;
    first = false;
    assert.ok(!calls.some(c=>c.method === "dispatch_reserved"));
    assert.match(message.text,/checking terminal acceptance/);
    assert.deepEqual(message.blocks.at(-1).buttons.map(b=>b.label),["检查状态"]);
  }});
  await bridge.click(`${bridge.id}:0`);
  assert.equal(first,false);
  assert.ok(bridge.calls.some(c=>c.method === "dispatch_reserved"));
});

test("failure to show initial progress cannot submit a reserved terminal write", async t => {
  const bridge = await runningBridge(t,{editFail:true});
  await bridge.click(`${bridge.id}:0`);
  assert.ok(bridge.calls.some(c=>c.method === "prepare_answer"));
  assert.ok(!bridge.calls.some(c=>c.method === "dispatch_reserved"));
  assert.match(bridge.edits.at(-1).payload.text,/edit offline/);
  assert.ok(!bridge.edits.some(edit=>edit.error));
});

test("a failed background edit does not block unrelated queued notifications", async t => {
  let count = 0;
  const id = "a".repeat(32);
  const bridge = await runningBridge(t,{events:[
    {id:"bad",type:"approval.updated",result:{request_id:id}},
    {id:"good",type:"approval.updated",result:{request_id:id}},
  ],update:async()=> {if (++count === 1) throw new Error("gone");}});
  for(let i=0;i<20 && !bridge.calls.some(c=>c.method === "ack");i++)
    await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(bridge.calls.filter(c=>c.method === "ack").map(c=>c.params.event_id),["good"]);
});

test("async question cards retain the full title/options and offer exact choices plus a freeform command", () => {
  const id = "c".repeat(32), full = "Long question option ".repeat(30);
  const payload = eventPayload({type:"question.requested",agent:{name:"pane",model:"codex",cwd:"/project"},
    summary:"Which label?",text:"• Working\n? 1 question",request:{id,expires_at:100,
      question:{title:"Which label?"},options:[{index:0,label:full},{index:1,label:"Green"}]}});
  assert.equal(payload.presentation.blocks[0].text,"`Which label?`");
  assert.equal(payload.presentation.blocks[1].text,"Which label?");
  assert.ok(payload.presentation.blocks[2].text.includes(full));
  assert.ok(payload.presentation.blocks.some(b=>b.text?.includes(`/herdr reply ${id}`)));
  assert.equal(payload.presentation.blocks.at(-1).buttons[1].action.value,`herdr-bridge:${id}:1`);
  const free = eventPayload({type:"question.requested",agent:{},text:"",request:{id,
    question:{title:"Your answer?"},options:[]}});
  assert.ok(!free.presentation.blocks.some(b=>b.type === "buttons"));
  assert.ok(free.presentation.blocks.some(b=>b.text?.includes(`/herdr reply ${id}`)));
});

test("question success requires recorded answer, not approval closure or a dispatch receipt", () => {
  assert.match(answerText({kind:"codex_question",option:"Purple",outcome:"confirmed"}),/Codex recorded the answer/);
  for (const outcome of ["checking","unconfirmed","uncertain","dispatched"])
    assert.ok(!answerText({kind:"codex_question",outcome}).includes("✅"));
});

test("owner freeform replies pass exact request/text and unauthorized commands never reach worker", async t => {
  const bridge = await runningBridge(t);
  await bridge.registrations.command.handler({...trusted,args:`reply ${bridge.id} custom\nanswer`});
  assert.deepEqual(bridge.calls.find(c=>c.method === "answer"),{
    method:"answer",params:{request_id:bridge.id,answer_text:"custom\nanswer"}});
  const count = bridge.calls.length;
  const denied = await bridge.registrations.command.handler({...trusted,senderId:"U2",args:`reply ${bridge.id} no`});
  assert.match(denied.text,/not authorized/);
  assert.equal(bridge.calls.length,count);
});

test("question choice shows checking before dispatch and confirms using question semantics", async t => {
  const bridge = await runningBridge(t,{result:{kind:"codex_question",option:"Purple"},onEdit:(message,calls)=> {
    if (!calls.some(c=>c.method === "dispatch_reserved")) assert.match(message.text,/checking question acceptance/);
  }});
  await bridge.click(`${bridge.id}:0`);
  assert.match(bridge.edits.at(-1).text,/question is still pending/);
  assert.equal(bridge.calls.filter(c=>c.method === "dispatch_reserved").length,1);
});

test("a text question reply without a bound interactive card still delivers its durable follow-up", async t => {
  const bridge = await runningBridge(t,{cards:[],result:{kind:"codex_question",outcome:"confirmed"},
    events:[{id:"text-answer",type:"approval.updated",result:{request_id:"a".repeat(32)}}]});
  for(let i=0;i<20 && !bridge.calls.some(c=>c.method === "ack");i++) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(bridge.edits.length,1);
  assert.match(bridge.edits[0].payload.text,/Codex recorded the answer/);
  assert.equal(bridge.calls.at(-1).method,"ack");
});

test("locally handled attention is rechecked immediately before sending and never delivered or acknowledged", async t => {
  const bridge = await runningBridge(t,{eventForDelivery:null,events:[{
    id:"handled",type:"approval.requested",agent:{pane_id:"w4:p2"},text:"approval",
    request:{id:"a".repeat(32),expires_at:100,options:[{index:0,label:"Yes"}]}
  }]});
  for(let i=0;i<20;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(bridge.edits.length,0);
  assert.ok(bridge.calls.some(c=>c.method === "event_for_delivery" && c.params.event_id === "handled"));
  assert.ok(!bridge.calls.some(c=>c.method === "ack"));
});

test("completion bypasses attention recheck while ready questions use the refreshed delivery event", async t => {
  const question = {id:"question",type:"question.requested",agent:{pane_id:"w4:p2"},text:"fresh evidence",
    request:{id:"a".repeat(32),question:{title:"A question"},expires_at:100,options:[{index:0,label:"Purple"}]}};
  const bridge = await runningBridge(t,{eventForDelivery:question,events:[
    {id:"question",type:"question.requested",text:"outdated evidence"},
    {id:"completion",type:"agent.done",agent:{pane_id:"w4:p2"},text:"finished"}
  ]});
  for(let i=0;i<20 && bridge.calls.filter(c=>c.method === "ack").length<2;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(bridge.edits.length,2);
  assert.ok(bridge.edits[0].payload.presentation.blocks.some(b=>b.text?.includes("fresh evidence")));
  assert.deepEqual(bridge.calls.filter(c=>c.method === "event_for_delivery").map(c=>c.params.event_id),["question"]);
  assert.deepEqual(bridge.calls.filter(c=>c.method === "ack").map(c=>c.params.event_id),["question","completion"]);
});

const channelConfig = {...config,paneChannels:{enabled:true,prefix:"herdr"}};
const channelFactory = ()=>({sync:async()=>{},identity:async()=>({team_id:"T1"})});

test("slow channel provisioning does not delay an existing channel's completion",async t=> {
  let release;
  const pending=new Promise(resolve=> {release=resolve;});
  const bridge=await runningBridge(t,{config:channelConfig,
    channelFactory:()=>({sync:()=>pending,identity:async()=>({team_id:"T1"})}),
    call:async(method)=> {
      if(method === "events") return [{id:"ready",type:"agent.done",agent:{pane_id:"w4:p2"},text:"done"}];
      if(method === "route_event") return {channel:"slack",accountId:"default",team_id:"T1",to:"channel:C2"};
      return {};
    }});
  try {
    for(let i=0;i<20 && !bridge.edits.length;i++) await new Promise(resolve=>setImmediate(resolve));
    assert.deepEqual(bridge.edits.map(e=>e.to),["channel:C2"]);
    assert.deepEqual(bridge.calls.filter(c=>c.method === "ack").map(c=>c.params.event_id),["ready"]);
  } finally {release();}
});

test("pane events use their own channels and only attention messages mention owners",async t=> {
  const events=[{id:"one",type:"agent.done",agent:{pane_id:"w4:p1"},text:"first"},
    {id:"two",type:"agent.blocked",agent:{pane_id:"w4:p2"},text:"second"}];
  const bridge=await runningBridge(t,{config:channelConfig,channelFactory,call:async(method,params)=> {
    if(method === "events") return events;
    if(method === "event_for_delivery") return events.find(e=>e.id === params.event_id);
    if(method === "route_event") return {channel:"slack",accountId:"default",team_id:"T1",to:params.event_id === "one" ? "channel:C1" : "channel:C2"};
    return {};
  }});
  for(let i=0;i<20 && bridge.edits.length<2;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(bridge.edits.map(e=>e.to),["channel:C1","channel:C2"]);
  assert.ok(!bridge.edits[0].payload.text.includes("<@U1>"));
  assert.ok(!JSON.stringify(bridge.edits[0].payload.presentation).includes("<@U1>"));
  assert.ok(bridge.edits[1].payload.text.startsWith("<@U1>\n"));
  assert.equal(bridge.edits[1].payload.presentation.blocks[0].text,"<@U1>");
  assert.equal(bridge.calls.filter(c=>c.method === "ack").length,2);
});

test("unprovisioned pane events stay queued while other ready panes deliver",async t=> {
  const events=[{id:"waiting",type:"agent.done",agent:{pane_id:"w4:p1"},text:"waiting"},
    {id:"ready",type:"agent.done",agent:{pane_id:"w4:p2"},text:"ready"}];
  const bridge=await runningBridge(t,{config:channelConfig,channelFactory,call:async(method,params)=> {
    if(method === "events") return events;
    if(method === "route_event") return params.event_id === "waiting" ? null : {channel:"slack",accountId:"default",team_id:"T1",to:"channel:C2"};
    return {};
  }});
  for(let i=0;i<20 && !bridge.edits.length;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(bridge.edits.map(e=>e.to),["channel:C2"]);
  assert.deepEqual(bridge.calls.filter(c=>c.method === "ack").map(c=>c.params.event_id),["ready"]);
});

test("channel commands infer their own pane and reject cross-pane or cross-request writes",async t=> {
  const bridge=await runningBridge(t,{config:channelConfig,channelFactory,call:async(method,params)=> {
    if(method === "events") return [];
    if(method === "pane_for_channel") return {pane_id:"w4:p2",channel_id:"C2",closed:false};
    if(method === "read") return {text:"pane two transcript"};
    if(method === "authorize_request") throw new Error("Request belongs to a different pane channel");
    return {};
  }});
  const handler=bridge.registrations.command.handler,ctx={...trusted,conversationId:"C2"};
  assert.match((await handler({...ctx,args:"read"})).text,/pane two transcript/);
  assert.equal(bridge.calls.find(c=>c.method === "read").params.pane,"w4:p2");
  const cross=await handler({...ctx,args:"send w4:p1 unsafe"});
  assert.match(cross.text,/different pane/);
  const reply=await handler({...ctx,args:`reply ${bridge.id} Purple`});
  assert.match(reply.text,/different pane channel/);
  assert.ok(!bridge.calls.some(c=>["send","answer","dispatch_reserved"].includes(c.method)));
});

test("channel recovery uses persisted source and account changes cannot deliver old events",async t=> {
  const events=[{id:"wrong-team",type:"agent.done",agent:{pane_id:"w4:p1"},text:"old"}];
  const bridge=await runningBridge(t,{config:channelConfig,channelFactory,call:async(method)=> {
    if(method === "events") return events;
    if(method === "route_event") return {channel:"slack",accountId:"default",team_id:"TOTHER",to:"channel:C1"};
    return {};
  }});
  for(let i=0;i<20;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(bridge.edits.length,0);assert.ok(!bridge.calls.some(c=>c.method === "ack"));
});

test("session binding and sync diagnostics are owner-only and scoped to the pane channel", async t => {
  const bridge = await runningBridge(t, {config:channelConfig,channelFactory,call:async(method)=> {
    if (method === "events") return [];
    if (method === "pane_for_channel") return {pane_id:"w4:p2",closed:false};
    return {state:"syncing",thread_id:"thread-a"};
  }});
  const handler = bridge.registrations.command.handler;
  const ctx = {...trusted,conversationId:"C2"};
  assert.match((await handler({...ctx,args:"bind thread-a"})).text,/syncing/);
  assert.deepEqual(bridge.calls.find(c=>c.method === "bind_session").params,{pane:"w4:p2",thread_id:"thread-a"});
  await handler({...ctx,args:"sync"});
  assert.deepEqual(bridge.calls.find(c=>c.method === "sync_status").params,{pane:"w4:p2"});
  const before = bridge.calls.filter(c=>c.method === "bind_session").length;
  assert.match((await handler({...ctx,args:"bind w4:p1 thread-b"})).text,/different pane/);
  assert.match((await handler({...ctx,senderId:"U2",args:"bind thread-b"})).text,/not authorized/);
  assert.equal(bridge.calls.filter(c=>c.method === "bind_session").length,before);
});
