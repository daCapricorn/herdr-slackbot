import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

export class BridgeError extends Error {
  constructor(message, { code = "bridge_error", details = {}, outcome = "unknown" } = {}) {
    super(message);
    this.name = "BridgeError";
    this.code = code;
    this.details = details;
    this.outcome = ["not_sent", "unknown"].includes(outcome) ? outcome : "unknown";
  }
}

// Each request is sent once. A timeout never retransmits terminal writes.
export class Worker {
  constructor(config, statePath, logger, spawnImpl = spawn) {
    this.config = config;
    this.statePath = statePath;
    this.logger = logger;
    this.spawnImpl = spawnImpl;
    this.pending = new Map();
    this.sequence = 0;
  }
  start() {
    const c = this.config;
    const args = ["-m", "herdr_bridge", "--state", this.statePath];
    if (c.socketPath) args.push("--socket", c.socketPath);
    if (c.herdrBin) args.push("--herdr-bin", c.herdrBin);
    this.child = this.spawnImpl(c.python, args, {
      cwd: c.projectRoot, stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONUNBUFFERED: "1" }, shell: false,
    });
    this.spawnFailed = false;
    this.lines = createInterface({ input: this.child.stdout });
    this.lines.on("line", (line) => {
      try {
        const msg = JSON.parse(line);
        const pending = this.pending.get(msg.id);
        if (!pending) return;
        this.pending.delete(msg.id);
        clearTimeout(pending.timer);
        if (msg.error) pending.reject(new BridgeError(msg.error.message, msg.error));
        else pending.resolve(msg.result);
      } catch { this.logger.warn("Herdr worker emitted invalid JSON"); }
    });
    this.errors = createInterface({ input: this.child.stderr });
    this.errors.on("line", (line) => this.logger.warn(`herdr-bridge: ${line}`));
    const fail = (code = "worker_disconnected", outcome = "unknown") => {
      for (const p of this.pending.values()) {
        clearTimeout(p.timer);
        p.reject(new BridgeError("Herdr worker disconnected; terminal effects may be uncertain", {
          code, outcome,
        }));
      }
      this.pending.clear();
    };
    this.child.on("error", () => {
      // Node leaves pid undefined when spawning fails. An error from an
      // already spawned child gives no proof about requests it has received.
      const failedToStart = this.child.pid === undefined;
      this.spawnFailed = true;
      fail(failedToStart ? "worker_start_failed" : "worker_disconnected", failedToStart ? "not_sent" : "unknown");
    });
    this.child.on("exit", () => fail());
    this.child.stdin.on("error", () => fail("worker_send_failed"));
  }
  call(method, params = {}) {
    if (!this.child || this.spawnFailed || this.child.exitCode !== null || this.child.killed)
      return Promise.reject(new BridgeError("Herdr worker is not running", {
        code: "worker_not_running", outcome: "not_sent",
      }));
    const id = `bridge-${++this.sequence}`;
    let payload;
    try { payload = JSON.stringify({ id, method, params }) + "\n"; }
    catch (error) {
      return Promise.reject(new BridgeError(error.message, { code: "worker_send_failed", outcome: "not_sent" }));
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new BridgeError("Herdr request timed out; do not repeat terminal writes without checking live state", {
          code: "worker_timeout", outcome: "unknown",
        }));
      }, 20000);
      this.pending.set(id, { resolve, reject, timer });
      const failed = (error) => {
        if (this.pending.delete(id)) {
          clearTimeout(timer);
          reject(new BridgeError(error.message, { code: "worker_send_failed", outcome: "unknown" }));
        }
      };
      try {
        this.child.stdin.write(payload, (error) => {
          if (error) failed(error);
        });
      } catch (error) { failed(error); }
    });
  }
  async stop() {
    if (!this.child) return;
    const child = this.child;
    if (child.exitCode !== null || this.spawnFailed) return;
    const exited = new Promise((resolve) => child.once("exit", resolve));
    child.stdin.end();
    const killTimer = setTimeout(() => child.kill("SIGTERM"), 5000);
    const forceTimer = setTimeout(() => child.kill("SIGKILL"), 10000);
    await exited;
    clearTimeout(killTimer);
    clearTimeout(forceTimer);
    this.lines.close();
    this.errors.close();
  }
}
