"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "..", "src", "kubelab", "static", "app.js"), "utf8");

class FakeElement {
  constructor() {
    this.attributes = new Map();
    this.classList = {
      values: new Set(["hidden"]),
      add: (value) => this.classList.values.add(value),
      remove: (value) => this.classList.values.delete(value),
    };
    this.dataset = {};
    this.disabled = true;
    this.listeners = new Map();
    this.textContent = "";
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

const runFailedInitialization = async (page) => {
  const root = new FakeElement();
  root.dataset.page = page;
  if (page === "lab-detail") root.dataset.labId = "lab-001";
  if (page === "session") root.dataset.sessionId = "session-001";

  const pageError = new FakeElement();
  const message = new FakeElement();
  const retryButton = new FakeElement();
  const busyButtons = [new FakeElement(), new FakeElement()];
  busyButtons.forEach((button) => button.setAttribute("aria-busy", "true"));
  const targets = new Map([
    ["[data-page]", root],
    ["#page-error", pageError],
    [page === "lab-detail" ? "#start-readiness" : "#session-task", message],
    [page === "lab-detail" ? "#start-lab" : "#reconcile-session", retryButton],
  ]);

  const document = {
    querySelector: (selector) => targets.get(selector) || null,
    querySelectorAll: (selector) =>
      selector === "button[aria-busy='true']" ? busyButtons : [],
  };
  const fetch = async (url) => {
    if (url === "/health") {
      return new Response("{}", {
        status: 200,
        headers: { "content-type": "application/json", "X-CSRF-Token": "test-token" },
      });
    }
    return new Response(JSON.stringify({ code: "TEST_FAILURE", message: "加载失败" }), {
      status: 500,
      headers: { "content-type": "application/json" },
    });
  };

  let reloaded = false;
  vm.runInNewContext(script, {
    document,
    fetch,
    Headers,
    navigator: {},
    Response,
    URL,
    URLSearchParams,
    window: {
      addEventListener: () => {},
      location: { href: "http://test.local/", reload: () => { reloaded = true; } },
      setInterval,
      setTimeout,
    },
  });
  await settle();
  await settle();
  return { busyButtons, message, pageError, retryButton, wasReloaded: () => reloaded };
};

for (const [page, retryMessage] of [
  ["lab-detail", "实验读取失败，请刷新页面重试。"],
  ["session", "Session 恢复失败，请刷新页面重试。"],
]) {
  test(`${page} initialization failure ends busy state and exposes retry guidance`, async () => {
    const { busyButtons, message, pageError, retryButton, wasReloaded } =
      await runFailedInitialization(page);

    assert.equal(message.textContent, retryMessage);
    assert.match(pageError.textContent, /加载失败/);
    assert.equal(pageError.classList.values.has("hidden"), false);
    assert.equal(retryButton.disabled, false);
    retryButton.listeners.get("click")();
    assert.equal(wasReloaded(), true);
    busyButtons.forEach((button) => {
      assert.equal(button.attributes.has("aria-busy"), false);
      assert.equal(button.disabled, true);
    });
  });
}
