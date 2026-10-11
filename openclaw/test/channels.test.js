import {test} from "node:test";
import assert from "node:assert/strict";
import {SlackPaneChannels,channelName,channelTopic,openClawBotToken} from "../channels.js";
import {updateAnswerCard} from "../presentation.js";

const config = {route:{channel:"slack",accountId:"default",to:"user:U1"},ownerIds:["U1"],paneChannels:{enabled:true,prefix:"herdr"}};

function fixture() {
  let binding = {id:"a".repeat(32),account_id:"default",pane_id:"w4:p2",terminal_id:"term-a",name:"codex-slackbot",
    cwd:"/project",state:"creating",channel_id:null,closed:false};
  const channels = [],calls = [],warnings = [],writes = [];
  let failure;
  const api = {runtime:{config:{current:()=>({channels:{slack:{botToken:"xoxb-test"}}})}},logger:{warn:s=>warnings.push(s)}};
  const call = async(method,params)=> {
    writes.push(method);
    if(method === "pane_channels") return [{...binding}];
    if(method === "channel_provisioned") binding = {...binding,channel_id:params.channel_id,team_id:params.team_id,state:"provisioning"};
    if(method === "channel_ready") binding = {...binding,state:"ready",channel_name:params.channel_name,topic:params.topic};
    if(method === "channel_archived") binding = {...binding,state:"archived"};
    return {...binding};
  };
  const fetcher = async(url,options)=> {
    const method = url.split("/").at(-1),params = Object.fromEntries(new URLSearchParams(options.body));
    assert.equal(options.headers.Authorization,"Bearer xoxb-test");
    assert.equal(options.headers["Content-Type"],"application/x-www-form-urlencoded");
    calls.push({method,params});
    if(failure) {const response = failure(method,params); if(response) return {ok:true,json:async()=>response};}
    let result = {ok:true};
    if(method === "auth.test") result = {...result,team_id:"T1",user_id:"UBOT"};
    if(method === "conversations.list") result = {...result,channels:structuredClone(channels)};
    if(method === "conversations.create") {
      const channel = {id:"C123",name:params.name,is_private:true,is_member:true,creator:"UBOT"};
      channels.push(channel);result = {...result,channel};
    }
    if(method === "conversations.members") result.members = ["UBOT"];
    return {ok:true,json:async()=>result};
  };
  const manager = new SlackPaneChannels(api,config,call,fetcher);
  return {manager,calls,writes,warnings,channels,api,get binding(){return binding;},set binding(b){binding=b;},set failure(f){failure=f;}};
}

test("channel names retain an identity suffix across renames and obey Slack naming limits",()=> {
  const binding = {id:"a".repeat(32),pane_id:"w4:p2",name:"Hello 中文 ".repeat(40)};
  const first = channelName(binding,"herdr");
  assert.ok(first.length <= 80 && /^[a-z0-9_-]+$/.test(first));
  assert.ok(first.endsWith(binding.id));
  assert.notEqual(first,channelName({...binding,name:"new-name"},"herdr"));
  assert.match(channelTopic({...binding,cwd:"/project",model:"gpt-6"}),/w4:p2.*gpt-6.*\/project/);
});

test("management reuses the live OpenClaw account credential and refuses unresolved secrets",()=> {
  assert.equal(openClawBotToken({channels:{slack:{botToken:"xoxb-base",accounts:{other:{botToken:"xoxb-other"}}}}},"other"),"xoxb-other");
  assert.throws(()=>openClawBotToken({channels:{slack:{botToken:{source:"env",id:"SECRET"}}}},"default"),/resolved/);
  assert.throws(()=>openClawBotToken({channels:{slack:{enabled:false,botToken:"xoxb-test"}}},"default"),/disabled/);
  assert.throws(()=>openClawBotToken({channels:{slack:{identity:"user",botToken:"xoxb-test"}}},"default"),/bot identity/);
});

test("creation invites the owner before readiness and idle scans perform no Slack calls",async()=> {
  const f = fixture();await f.manager.sync();
  assert.equal(f.binding.state,"ready");
  assert.deepEqual(f.calls.map(c=>c.method),["auth.test","conversations.list","conversations.create","conversations.members",
    "conversations.invite","conversations.setPurpose","conversations.setTopic"]);
  assert.deepEqual(f.calls.find(c=>c.method === "conversations.invite").params,{channel:"C123",users:"U1"});
  assert.deepEqual(f.writes,["pane_channels","channel_provisioned","channel_ready"]);
  f.calls.length=0;f.writes.length=0;await f.manager.sync();
  assert.deepEqual(f.calls,[]);assert.deepEqual(f.writes,["pane_channels"]);
});

test("rename keeps channel ID and only changed metadata makes management calls",async()=> {
  const f = fixture();await f.manager.sync();f.calls.length=0;
  f.binding={...f.binding,name:"renamed",model:"gpt-6"};await f.manager.sync();
  assert.equal(f.binding.channel_id,"C123");
  assert.deepEqual(f.calls.map(c=>c.method),["conversations.rename","conversations.setTopic"]);
  assert.match(f.binding.channel_name,/^herdr-renamed-/);
});

test("a created channel is adopted after a crash before its ID was persisted",async()=> {
  const f = fixture();
  f.channels.push({id:"C123",name:channelName(f.binding),creator:"UBOT",is_private:true,is_member:true});
  await f.manager.sync();assert.equal(f.binding.channel_id,"C123");
  assert.ok(!f.calls.some(c=>c.method === "conversations.create"));
});

test("an unrelated same-name channel is never adopted and does not mark the pane ready",async()=> {
  const f = fixture();f.channels.push({id:"COTHER",name:channelName(f.binding),creator:"UOTHER",is_private:true,is_member:true});
  await f.manager.sync();assert.equal(f.binding.channel_id,null);
  assert.match(f.warnings[0],/unrelated/);assert.ok(!f.calls.some(c=>c.method === "conversations.create"));
});

test("invitation failure retains the created ID and retries provisioning without creating again",async()=> {
  const f = fixture();f.failure=method=>method === "conversations.invite" ? {ok:false,error:"missing_scope",needed:"groups:write.invites"} : null;
  await f.manager.sync();assert.equal(f.binding.channel_id,"C123");assert.equal(f.binding.state,"provisioning");
  f.failure=null;await f.manager.sync();assert.equal(f.binding.state,"ready");
  assert.equal(f.calls.filter(c=>c.method === "conversations.create").length,1);
});

test("failed reconciliation reads never lead to blind channel creation",async()=> {
  const f = fixture();f.failure=method=>method === "conversations.list" ? {ok:false,error:"internal_error"} : null;
  await f.manager.sync();assert.equal(f.binding.channel_id,null);
  assert.ok(!f.calls.some(c=>c.method === "conversations.create"));
});

test("a workspace change holds existing bindings and a rename failure retains routing readiness",async()=> {
  const f = fixture();await f.manager.sync();f.calls.length=0;
  f.binding={...f.binding,name:"new-name"};f.failure=method=>method === "conversations.rename" ? {ok:false,error:"internal_error"} : null;
  await f.manager.sync();assert.equal(f.binding.state,"ready");assert.equal(f.binding.channel_id,"C123");
  f.binding={...f.binding,team_id:"TOTHER"};f.calls.length=0;await f.manager.sync();
  assert.deepEqual(f.calls,[]);assert.match(f.warnings.at(-1),/workspace changed/);
});

test("closed channels retain history until explicit archival and archive retries converge",async()=> {
  const f = fixture();await f.manager.sync();
  await assert.rejects(f.manager.archive(f.binding),/Close/);
  f.binding={...f.binding,closed:true};f.calls.length=0;await f.manager.sync();assert.deepEqual(f.calls,[]);
  f.failure=method=>method === "conversations.archive" ? {ok:false,error:"already_archived"} : null;
  await f.manager.archive(f.binding);assert.equal(f.binding.state,"archived");
});

test("recovery uses the original channel and rejects a different channel destination",async()=> {
  const binding={channel:"slack",accountId:"default",conversationId:"C123",messageTs:"123.456"};
  const route={...config.route,to:"channel:C123"};
  const delivered=[];
  const api={runtime:{config:{current:()=>({})},channel:{outbound:{loadAdapter:async()=>({sendPayload:ctx=>delivered.push(ctx)})}}}};
  await updateAnswerCard(api,route,binding,{request_id:"a".repeat(32),outcome:"confirmed"});
  assert.equal(delivered[0].to,"channel:C123");
  await assert.rejects(updateAnswerCard(api,{...route,to:"channel:C456"},binding,{outcome:"confirmed"}),/conversation route/);
});
