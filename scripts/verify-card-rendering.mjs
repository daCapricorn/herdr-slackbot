// Offline integration check against the installed OpenClaw Slack adapter.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { answerPayload, answerEdit } from "../openclaw/presentation.js";

const adapterPath = process.argv[2];
if (!adapterPath) throw new Error("Usage: node scripts/verify-card-rendering.mjs <Slack outbound adapter module>");
const { slackOutbound: adapter } = await import(pathToFileURL(adapterPath));
const api = {runtime:{config:{current:()=>({})},channel:{outbound:{loadAdapter:async()=>adapter}}}};
const context = {type:"question.requested",agent:{name:"pane-name",model:"model",cwd:"/project"},
  text:"ORIGINAL CHAT HISTORY\n$ uname -s\nDarwin",summary:"Original summary",
  request:{id:"a".repeat(32),expires_at:200,options:[{index:0,label:"Yes"}],question:{title:"Original question"}}};
for (const outcome of ["checking","unconfirmed","confirmed"]) {
  const result={request_id:context.request.id,kind:"codex_question",option:"Yes",outcome,card_context:context};
  const edit=await answerEdit(api,{channel:"slack",accountId:"default",to:"channel:C1"},result);
  assert.ok(edit.blocks.some(b=>b.type === "header"));
  assert.ok(JSON.stringify(edit.blocks).includes("ORIGINAL CHAT HISTORY"));
  assert.ok(JSON.stringify(edit.blocks).includes("Original summary"));
  assert.equal(edit.blocks.some(b=>b.type === "actions"),outcome !== "confirmed");
  // Restart recovery uses the same portable payload through outbound delivery.
  const payload=answerPayload(result);
  const rendered=await adapter.renderPresentation({payload});
  const segments=rendered.channelData.slack.renderedPresentationSegments;
  assert.ok(segments.every(s=>s.kind === "blocks"));
  assert.ok(JSON.stringify(segments).includes("ORIGINAL CHAT HISTORY"));
}
console.log("Actual OpenClaw Slack renderer: checking, confirmation and recovery retain native context blocks.");
