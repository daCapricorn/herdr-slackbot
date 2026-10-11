import {readFileSync} from "node:fs";
import {DatabaseSync} from "node:sqlite";
import {SlackPaneChannels,channelName} from "../openclaw/channels.js";

const [configPath,statePath] = process.argv.slice(2);
if (!configPath || !statePath) throw new Error("Usage: node scripts/verify-pane-channels.mjs <OpenClaw config> <Bridge SQLite>");
const cfg = JSON.parse(readFileSync(configPath,"utf8"));
const config = cfg.plugins.entries["herdr-bridge"].config;
const api = {runtime:{config:{current:()=>cfg}},logger:{warn(){}}};
const manager = new SlackPaneChannels(api,config,()=>{throw new Error("Read-only verifier cannot mutate Bridge state");});
const db = new DatabaseSync(statePath,{readOnly:true});
const rows = db.prepare("SELECT record FROM pane_channels WHERE account_id=?").all(config.route.accountId)
  .map(r=>JSON.parse(r.record)).filter(r=>!r.closed);
const evidence=[];
try {
  for (const binding of rows) {
    if (binding.state !== "ready") throw new Error(`Pane ${binding.pane_id} is not ready`);
    const {channel} = await manager.request("conversations.info",{channel:binding.channel_id});
    let cursor;
    const members=[];
    do {
      const page=await manager.request("conversations.members",{channel:binding.channel_id,limit:200,...(cursor ? {cursor} : {})});
      members.push(...page.members);cursor=page.response_metadata?.next_cursor;
    } while(cursor);
    if (!channel.is_private || channel.name !== channelName(binding,config.paneChannels.prefix || "herdr")
        || !config.ownerIds.every(id=>members.includes(id))) throw new Error(`Pane ${binding.pane_id} channel metadata/membership mismatch`);
    evidence.push({pane_id:binding.pane_id,terminal_id:binding.terminal_id,channel_id:channel.id,
      name:channel.name,private:channel.is_private,owners_present:true});
  }
  console.log(JSON.stringify({ok:true,bindings:evidence},null,2));
} finally {db.close();}
