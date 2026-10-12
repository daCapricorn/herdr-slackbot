// Read-only Slack evidence: no token output, message edits, or terminal input.
import {readFileSync} from "node:fs";
import {SlackPaneChannels} from "../openclaw/channels.js";
const [configPath, channel, marker] = process.argv.slice(2);
if (!configPath || !channel || !marker) throw new Error("Usage: node scripts/verify-agent-sync.mjs <OpenClaw config> <channel> <marker>");
const cfg = JSON.parse(readFileSync(configPath,"utf8"));
const api = {runtime:{config:{current:()=>cfg}},logger:{warn(){}}};
const manager = new SlackPaneChannels(api,cfg.plugins.entries["herdr-bridge"].config,()=>{throw new Error("Read-only verifier");});
const page = await manager.request("conversations.history", {channel, limit:100});
const matches = page.messages.filter(m => JSON.stringify(m).includes(marker));
const evidence = matches.map(m=>({ts:m.ts,role:/^(?:👤|:bust_in_silhouette:) You/.test(m.text || "") ? "user" : /^(?:🤖|:robot_face:)/.test(m.text || "") ? "assistant" : "notification"}));
console.log(JSON.stringify({channel,marker,evidence},null,2));
if (!evidence.some(e=>e.role==="user") || !evidence.some(e=>e.role==="assistant")) process.exitCode=1;
