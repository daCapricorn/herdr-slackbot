import { createHash } from "node:crypto";

export function channelName(binding, prefix = "herdr") {
  const slug = String(binding.name || binding.pane_id).normalize("NFKD").toLowerCase()
    .replace(/[^a-z0-9_-]+/g,"-").replace(/^-+|-+$/g,"") || "pane";
  return `${prefix}-${slug.slice(0,Math.max(1,46-prefix.length))}-${binding.id}`.slice(0,80);
}

export function channelTopic(binding) {
  const name = String(binding.name || binding.pane_id).slice(0,60);
  const model = String(binding.model || "模型未知").slice(0,50);
  return `${name} · ${binding.pane_id} · ${model} · ${binding.cwd || "目录未知"}`.slice(0,250);
}

export function openClawBotToken(cfg, accountId) {
  const slack = cfg.channels?.slack;
  const account = slack?.accounts?.[accountId];
  if (!slack || slack.enabled === false || account?.enabled === false)
    throw new Error("OpenClaw Slack account is disabled");
  if ((account?.identity ?? slack.identity) === "user")
    throw new Error("Pane channel management requires OpenClaw's bot identity");
  if (slack.accounts && !account && accountId !== "default")
    throw new Error("OpenClaw Slack account is not configured");
  const token = account?.botToken ?? slack.botToken ?? (accountId === "default" ? process.env.SLACK_BOT_TOKEN : undefined);
  if (typeof token !== "string" || !token.startsWith("xoxb-"))
    throw new Error("Pane channel management requires a resolved OpenClaw Slack bot token");
  return token;
}

export class SlackPaneChannels {
  constructor(api, config, call, fetcher = globalThis.fetch) {
    this.api = api;
    this.config = config;
    this.call = call;
    this.fetch = fetcher;
  }

  async request(method, params = {}, signal) {
    const token = openClawBotToken(this.api.runtime.config.current(), this.config.route.accountId);
    const response = await this.fetch(`https://slack.com/api/${method}`, {
      method:"POST", headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/x-www-form-urlencoded"},
      body:new URLSearchParams(Object.entries(params).map(([key,value])=>[key,String(value)])),
      signal:signal ? AbortSignal.any([signal,AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error(`Slack channel management HTTP ${response.status}`);
    const result = await response.json();
    if (!result.ok) {
      const reason = /^[a-z_]+$/.test(result.error) ? result.error : "request_failed";
      throw new Error(`Slack ${method}: ${reason}${reason === "missing_scope" ? " (" + (result.needed || "channel management permission") + ")" : ""}`);
    }
    return result;
  }

  async identity(signal) {
    const digest = createHash("sha256").update(openClawBotToken(this.api.runtime.config.current(),this.config.route.accountId)).digest("hex");
    if (this.identityDigest !== digest) {
      const auth = await this.request("auth.test",{},signal);
      this.auth = {team_id:auth.team_id,user_id:auth.user_id};
      this.identityDigest = digest;
    }
    return this.auth;
  }

  async findCreated(name, owner, signal) {
    let cursor;
    do {
      const page = await this.request("conversations.list",{types:"private_channel",exclude_archived:true,limit:200,...(cursor ? {cursor} : {})},signal);
      const found = page.channels.find(c=>c.name === name);
      if (found) {
        if (!found.is_private || found.creator !== owner || !found.is_member)
          throw new Error("Channel name is occupied by an unrelated conversation");
        return found.id;
      }
      cursor = page.response_metadata?.next_cursor;
    } while (cursor);
    return null;
  }

  async sync(signal) {
    const bindings = await this.call("pane_channels",{account_id:this.config.route.accountId});
    const auth = await this.identity(signal);
    for (let binding of bindings) {
      if (signal?.aborted) break;
      if (binding.closed || binding.state === "archived") continue;
      try {
        if (binding.team_id && binding.team_id !== auth.team_id)
          throw new Error("Slack account workspace changed; refusing to rebind pane channels");
        const name = channelName(binding,this.config.paneChannels?.prefix || "herdr");
        if (!binding.channel_id) {
          let id = await this.findCreated(name,auth.user_id,signal);
          if (!id) id = (await this.request("conversations.create",{name,is_private:true},signal)).channel.id;
          binding = await this.call("channel_provisioned",{binding_id:binding.id,channel_id:id,team_id:auth.team_id});
        }
        if (binding.state !== "ready") {
          const members = [];
          let cursor;
          do {
            const page = await this.request("conversations.members",{channel:binding.channel_id,limit:200,...(cursor ? {cursor} : {})},signal);
            members.push(...page.members);
            cursor = page.response_metadata?.next_cursor;
          } while (cursor);
          const missing = this.config.ownerIds.filter(id=>!members.includes(id));
          if (missing.length) await this.request("conversations.invite",{channel:binding.channel_id,users:missing.join(",")},signal);
          await this.request("conversations.setPurpose",{channel:binding.channel_id,purpose:`Herdr Bridge pane binding ${binding.id}`},signal);
        }
        const topic = channelTopic(binding);
        // Rename/topic failures do not disable an already usable destination.
        if (binding.channel_name && binding.channel_name !== name)
          await this.request("conversations.rename",{channel:binding.channel_id,name},signal);
        if (binding.topic !== topic)
          await this.request("conversations.setTopic",{channel:binding.channel_id,topic},signal);
        if (binding.state !== "ready" || binding.channel_name !== name || binding.topic !== topic)
          await this.call("channel_ready",{binding_id:binding.id,channel_name:name,topic});
      } catch (error) {
        this.api.logger.warn(`Herdr pane channel deferred: ${binding.pane_id} ${error.message}`);
      }
    }
    return bindings;
  }

  async archive(binding, signal) {
    if (!binding.closed) throw new Error("Close the Herdr pane before archiving its channel");
    const auth = await this.identity(signal);
    if (auth.team_id !== binding.team_id) throw new Error("Slack account workspace changed");
    try { await this.request("conversations.archive",{channel:binding.channel_id},signal); }
    catch (error) { if (!error.message.endsWith(": already_archived")) throw error; }
    await this.call("channel_archived",{binding_id:binding.id});
  }
}
