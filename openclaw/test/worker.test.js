import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { Worker } from "../worker.js";

function fixture(t, write) {
  const child = new EventEmitter();
  Object.assign(child, { pid: 42, exitCode: null, killed: false, stdout: new PassThrough(), stderr: new PassThrough() });
  child.stdin = new EventEmitter();
  const writes = [];
  child.stdin.write = (line, callback) => {
    writes.push(JSON.parse(line));
    return write ? write(line, callback) : (callback(), true);
  };
  const worker = new Worker({ python: "python", projectRoot: "/tmp" }, "/tmp/test.sqlite", { warn() {} }, () => child);
  worker.start();
  t.after(() => {
    child.emit("exit");
    worker.lines.close();
    worker.errors.close();
    child.stdout.destroy();
    child.stderr.destroy();
  });
  return { worker, child, writes };
}

test("Python errors retain code, details and terminal outcome across Worker", async t => {
  const { worker, child, writes } = fixture(t);
  const result = worker.call("dispatch_reserved", { request_id: "request", attempt_id: "attempt" });
  child.stdout.write(JSON.stringify({ id: writes[0].id, error: {
    code: "stale_submission", message: "Submission is stale; nothing was sent",
    details: { request_id: "request", attempt_id: "attempt" }, outcome: "not_sent",
  } }) + "\n");
  await assert.rejects(result, error => {
    assert.equal(error.message, "Submission is stale; nothing was sent");
    assert.equal(error.code, "stale_submission");
    assert.deepEqual(error.details, { request_id: "request", attempt_id: "attempt" });
    assert.equal(error.outcome, "not_sent");
    return true;
  });
  assert.equal(writes.length, 1);
});

test("legacy Python errors and acknowledgement results gain no terminal proof", async t => {
  const { worker, child, writes } = fixture(t);
  let result = worker.call("send");
  child.stdout.write(JSON.stringify({ id: writes[0].id, error: { code: "unavailable", message: "gone" } }) + "\n");
  await assert.rejects(result, error => error.code === "unavailable" && error.outcome === "unknown");
  result = worker.call("send");
  child.stdout.write(JSON.stringify({ id: writes[1].id, result: { acknowledged: true, outcome: "uncertain" } }) + "\n");
  assert.deepEqual(await result, { acknowledged: true, outcome: "uncertain" });
  assert.equal(writes.length, 2);
});

test("worker unavailable and serialization failures prove this invocation was not sent", async t => {
  const worker = new Worker({}, "", { warn() {} });
  await assert.rejects(worker.call("send"), error => error.code === "worker_not_running" && error.outcome === "not_sent");
  const { worker: running, writes } = fixture(t);
  const params = {}; params.self = params;
  await assert.rejects(running.call("send", params), error => error.code === "worker_send_failed" && error.outcome === "not_sent");
  assert.equal(writes.length, 0);
  assert.equal(running.pending.size, 0);
});

test("failed spawn is not sent; errors after spawning remain unknown", async t => {
  for (const spawned of [false, true]) {
    const { worker, child, writes } = fixture(t);
    if (!spawned) delete child.pid;
    const result = worker.call("send");
    child.emit("error", new Error("spawn or process error"));
    await assert.rejects(result, error => error.code === (spawned ? "worker_disconnected" : "worker_start_failed")
      && error.outcome === (spawned ? "unknown" : "not_sent"));
    assert.equal(writes.length, 1);
    assert.equal(worker.pending.size, 0);
  }
});

test("write callback errors and synchronous throws cannot prove no terminal effect", async t => {
  for (const synchronous of [false, true]) {
    const { worker, writes } = fixture(t, (_, callback) => {
      if (synchronous) throw new Error("write failed");
      callback(new Error("write failed"));
    });
    await assert.rejects(worker.call("send"), error => error.code === "worker_send_failed"
      && error.message === "write failed" && error.outcome === "unknown");
    assert.equal(writes.length, 1);
    assert.equal(worker.pending.size, 0);
  }
});

test("timeout, stream failure and disconnect never retransmit or settle twice", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  for (const failure of ["timeout", "stdin", "exit"]) {
    const { worker, child, writes } = fixture(t);
    const result = worker.call("dispatch_reserved");
    if (failure === "timeout") t.mock.timers.tick(20000);
    else if (failure === "stdin") child.stdin.emit("error", new Error("pipe failed"));
    else child.emit("exit", 1);
    await assert.rejects(result, error => error.code === ({ timeout: "worker_timeout", stdin: "worker_send_failed", exit: "worker_disconnected" })[failure]
      && error.outcome === "unknown");
    child.stdout.write(JSON.stringify({ id: writes[0].id, result: { outcome: "confirmed" } }) + "\n");
    t.mock.timers.tick(20000);
    assert.equal(writes.length, 1);
    assert.equal(worker.pending.size, 0);
  }
});
