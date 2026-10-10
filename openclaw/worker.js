import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

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
        if (msg.error) pending.reject(new Error(msg.error.message));
        else pending.resolve(msg.result);
      } catch { this.logger.warn("Herdr worker emitted invalid JSON"); }
    });
    this.errors = createInterface({ input: this.child.stderr });
    this.errors.on("line", (line) => this.logger.warn(`herdr-bridge: ${line}`));
    const fail = () => {
      for (const p of this.pending.values()) {
        clearTimeout(p.timer);
        p.reject(new Error("Herdr worker disconnected; terminal effects may be uncertain"));
      }
      this.pending.clear();
    };
    this.child.on("error", () => { this.spawnFailed = true; fail(); });
    this.child.on("exit", fail);
    this.child.stdin.on("error", fail);
  }
  call(method, params = {}) {
    if (!this.child || this.spawnFailed || this.child.exitCode !== null || this.child.killed)
      return Promise.reject(new Error("Herdr worker is not running"));
    const id = `bridge-${++this.sequence}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("Herdr request timed out; do not repeat terminal writes without checking live state"));
      }, 20000);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(JSON.stringify({ id, method, params }) + "\n", (error) => {
        if (error && this.pending.delete(id)) { clearTimeout(timer); reject(error); }
      });
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
