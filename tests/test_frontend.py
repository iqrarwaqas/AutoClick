"""Frontend static integrity and JS logic tests for AutoClick."""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "templates" / "index.html"
JS_PATH = ROOT / "static" / "app.js"
CSS_PATH = ROOT / "static" / "style.css"
NODE_BIN = shutil.which("node")


def _extract_js_function_body(source: str, func_name: str) -> str:
    """Extract the brace-balanced body of `function <func_name>(...) { ... }`."""
    match = re.search(rf"\bfunction\s+{re.escape(func_name)}\s*\([^)]*\)\s*\{{", source)
    if not match:
        raise AssertionError(f"Function {func_name!r} not found in static/app.js")
    start = match.end()
    depth = 1
    i = start
    in_str: str | None = None
    in_line_comment = False
    in_block_comment = False
    while i < len(source) and depth > 0:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 1
        elif in_str:
            if ch == "\\":
                i += 1
            elif ch == in_str:
                in_str = None
        else:
            if ch == "/" and nxt == "/":
                in_line_comment = True
                i += 1
            elif ch == "/" and nxt == "*":
                in_block_comment = True
                i += 1
            elif ch in ('"', "'", "`"):
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return source[start:i]
        i += 1
    raise AssertionError(f"Unbalanced braces in function {func_name!r}")


# Lightweight Node.js VM harness that loads the real `static/app.js` without
# external npm dependencies, providing a mock DOM, mock fetch, and fake timers.
_NODE_HARNESS = textwrap.dedent(
    r"""
    const fs = require("fs");
    const vm = require("vm");
    const assert = require("assert");

    function createAppSandbox(options = {}) {
      const appSource = fs.readFileSync(options.jsPath, "utf8");
      const elementsById = new Map();
      const docListeners = new Map();
      const winListeners = new Map();

      function makeClassList(el) {
        const set = new Set();
        return {
          _set: set,
          add(...cls) { cls.forEach((c) => c && set.add(c)); el._syncClassName(); },
          remove(...cls) { cls.forEach((c) => set.delete(c)); el._syncClassName(); },
          contains(c) { return set.has(c); },
          toggle(c, force) {
            const on = force === undefined ? !set.has(c) : Boolean(force);
            if (on) set.add(c); else set.delete(c);
            el._syncClassName();
            return on;
          },
        };
      }

      class FakeElement {
        constructor(tagName = "div", id = "") {
          this.tagName = String(tagName).toUpperCase();
          this.id = id;
          this.attributes = {};
          this.dataset = {};
          this.style = {};
          this.children = [];
          this.childNodes = [];
          this.parentNode = null;
          this._textContent = "";
          this._innerHTML = "";
          this.value = "";
          this.checked = false;
          this.disabled = false;
          this.selected = false;
          this.draggable = false;
          this.title = "";
          this.type = "";
          this.min = "";
          this.max = "";
          this.step = "";
          this.href = "";
          this.download = "";
          this.scrollTop = 0;
          this.scrollHeight = 0;
          this.files = [];
          this._listeners = new Map();
          this.classList = makeClassList(this);
          this._rect = { top: 0, bottom: 40, left: 0, right: 400, width: 400, height: 40 };
        }
        _syncClassName() {
          this._className = Array.from(this.classList._set).join(" ");
        }
        get className() { return this._className || ""; }
        set className(val) {
          this.classList._set.clear();
          String(val || "").split(/\s+/).filter(Boolean).forEach((c) => this.classList._set.add(c));
          this._syncClassName();
        }
        get textContent() { return this._textContent; }
        set textContent(v) { this._textContent = String(v ?? ""); }
        get innerHTML() { return this._innerHTML; }
        set innerHTML(v) { this._innerHTML = String(v ?? ""); }
        get lastElementChild() {
          return this.children.length ? this.children[this.children.length - 1] : null;
        }
        setAttribute(k, v) { this.attributes[k] = String(v); }
        getAttribute(k) { return this.attributes[k] ?? null; }
        append(...nodes) {
          for (const n of nodes) {
            if (n == null) continue;
            if (typeof n === "string") {
              this.childNodes.push({ nodeType: 3, textContent: n });
            } else {
              if (n instanceof FakeElement) {
                n.parentNode = this;
                this.children.push(n);
              }
              this.childNodes.push(n);
            }
          }
        }
        replaceChildren(...nodes) {
          this.children.forEach((c) => { c.parentNode = null; });
          this.children = [];
          this.childNodes = [];
          this.append(...nodes);
        }
        addEventListener(type, fn) {
          if (!this._listeners.has(type)) this._listeners.set(type, []);
          this._listeners.get(type).push(fn);
        }
        dispatchEvent(ev) {
          const event = Object.assign(
            {
              type: "",
              target: this,
              currentTarget: this,
              defaultPrevented: false,
              propagationStopped: false,
              preventDefault() { this.defaultPrevented = true; },
              stopPropagation() { this.propagationStopped = true; },
            },
            ev
          );
          const list = this._listeners.get(event.type) || [];
          for (const fn of list) fn(event);
          return event;
        }
        click() {
          if (this.disabled) return;
          this.dispatchEvent({ type: "click" });
        }
        getBoundingClientRect() { return this._rect; }
        _walk(out = []) {
          for (const c of this.children) {
            out.push(c);
            c._walk(out);
          }
          return out;
        }
        querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
        querySelectorAll(sel) {
          return matchSelector(this._walk(), sel);
        }
      }

      function matchesSingle(el, part) {
        part = part.trim();
        if (!part) return false;
        if (part === "tr[data-step-id]") {
          return el.tagName === "TR" && Object.prototype.hasOwnProperty.call(el.dataset, "stepId");
        }
        if (part === "tr.drag-over-top") return el.tagName === "TR" && el.classList.contains("drag-over-top");
        if (part === "tr.drag-over-bottom") return el.tagName === "TR" && el.classList.contains("drag-over-bottom");
        const optMatch = part.match(/^option\[value="([^"]*)"\]$/);
        if (optMatch) return el.tagName === "OPTION" && el.value === optMatch[1];
        if (part.startsWith(".")) return el.classList.contains(part.slice(1));
        if (part.startsWith("[data-")) {
          const attr = part.slice(1, -1);
          const camel = attr.replace(/^data-/, "").replace(/-([a-z])/g, (_, c) => c.toUpperCase());
          return Object.prototype.hasOwnProperty.call(el.dataset, camel);
        }
        const tagCls = part.match(/^([a-z]+)\.([a-z0-9_-]+)$/i);
        if (tagCls) return el.tagName === tagCls[1].toUpperCase() && el.classList.contains(tagCls[2]);
        const tagAttr = part.match(/^([a-z]+)\[data-([a-z0-9_-]+)="([^"]*)"\]$/i);
        if (tagAttr) {
          const camel = tagAttr[2].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
          return el.tagName === tagAttr[1].toUpperCase() && el.dataset[camel] === tagAttr[3];
        }
        return el.tagName === part.toUpperCase();
      }

      function matchSelector(nodes, selector) {
        const groups = selector.split(",").map((s) => s.trim()).filter(Boolean);
        const results = [];
        for (const group of groups) {
          const descendant = group.match(/^#([a-z0-9_-]+)\s+(.+)$/i);
          let pool = nodes;
          let leafSel = group;
          if (descendant) {
            const root = elementsById.get(descendant[1]);
            pool = root ? root._walk() : [];
            leafSel = descendant[2];
          }
          for (const el of pool) {
            if (matchesSingle(el, leafSel) && !results.includes(el)) results.push(el);
          }
        }
        return results;
      }

      function getOrCreateById(id) {
        if (!elementsById.has(id)) {
          const el = new FakeElement("div", id);
          if (id === "menu-update" || id === "home-update") {
            const span = new FakeElement("span");
            span.textContent = "Check for updates";
            el.append(span);
          }
          if (id === "m-type") {
            ["hotkey", "key", "type", "click", "wait", "group"].forEach((t, i) => {
              const btn = new FakeElement("button");
              btn.dataset.type = t;
              if (i === 0) btn.classList.add("active");
              el.append(btn);
            });
          }
          elementsById.set(id, el);
        }
        return elementsById.get(id);
      }

      function allNodes() {
        const out = [];
        const seen = new Set();
        for (const el of elementsById.values()) {
          if (!seen.has(el)) { seen.add(el); out.push(el); }
          for (const child of el._walk()) {
            if (!seen.has(child)) { seen.add(child); out.push(child); }
          }
        }
        return out;
      }

      const document = {
        documentElement: new FakeElement("html"),
        body: new FakeElement("body"),
        title: "AutoClick",
        getElementById: getOrCreateById,
        createElement: (tag) => new FakeElement(tag),
        createTextNode: (txt) => ({ nodeType: 3, textContent: String(txt) }),
        querySelector: (sel) => matchSelector(allNodes(), sel)[0] || null,
        querySelectorAll: (sel) => matchSelector(allNodes(), sel),
        addEventListener(type, fn) {
          if (!docListeners.has(type)) docListeners.set(type, []);
          docListeners.get(type).push(fn);
        },
        dispatchEvent(ev) {
          const list = docListeners.get(ev.type) || [];
          for (const fn of list) fn(ev);
        },
      };

      const makeStorage = () => {
        const store = new Map();
        return {
          getItem: (k) => (store.has(k) ? store.get(k) : null),
          setItem: (k, v) => { store.set(k, String(v)); },
          removeItem: (k) => { store.delete(k); },
          clear: () => { store.clear(); },
        };
      };

      // Controllable fake timers so tests never sleep or hang on intervals.
      let nextTimerId = 1;
      const timeouts = new Map();
      const intervals = new Map();

      function fakeSetTimeout(fn, delay = 0) {
        const id = nextTimerId++;
        timeouts.set(id, { fn, delay });
        return id;
      }
      function fakeClearTimeout(id) { timeouts.delete(id); }
      function fakeSetInterval(fn, delay = 0) {
        const id = nextTimerId++;
        intervals.set(id, { fn, delay });
        return id;
      }
      function fakeClearInterval(id) { intervals.delete(id); }

      function flushTimeouts() {
        const entries = Array.from(timeouts.entries());
        timeouts.clear();
        for (const [, entry] of entries) entry.fn();
      }

      const defaultState = options.initialState || {
        presets: [
          {
            id: "p1",
            name: "Test Preset",
            start_delay: 5,
            iterations: 3,
            infinite: false,
            steps: [
              { id: "s1", action: "hotkey", keys: ["ctrl", "l"], hold_ms: 0, delay: 0.1, enabled: true, note: "" },
              { id: "s2", action: "hotkey", keys: ["ctrl", "c"], hold_ms: 0, delay: 0.1, enabled: true, note: "" },
              { id: "s3", action: "key", key: "enter", presses: 1, hold_ms: 0, delay: 0.1, enabled: true, note: "" },
            ],
          },
        ],
        active_preset_id: "p1",
        settings: { stop_key: "f8", record_stop_key: "f9", failsafe: true, minimize_on_start: false, speed: 1.0 },
        runner: { status: "idle", active: false, iteration: 0, total_iterations: 0, steps_completed: 0, total_steps: 0, elapsed: 0, countdown_remaining: 0, logs: [] },
        recorder: { status: "idle", active: false, step_count: 0, steps: [], elapsed: 0, cooloff_remaining: 0, logs: [] },
      };

      let fetchHandler = options.fetchHandler || (async (url, opts) => {
        if (url === "/api/state") return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(defaultState)) };
        if (url.startsWith("/api/update")) return { ok: true, status: 200, json: async () => ({ available: false, current: "1.0.0", latest: "1.0.0" }) };
        return { ok: true, status: 200, json: async () => ({ ok: true }) };
      });

      const windowObj = {
        pywebview: null,
        addEventListener(type, fn) {
          if (!winListeners.has(type)) winListeners.set(type, []);
          winListeners.get(type).push(fn);
        },
      };

      const sandbox = {
        console,
        assert,
        document,
        window: windowObj,
        localStorage: makeStorage(),
        sessionStorage: makeStorage(),
        matchMedia: () => ({ matches: false, addEventListener() {} }),
        fetch: (url, opts) => fetchHandler(url, opts),
        setTimeout: fakeSetTimeout,
        clearTimeout: fakeClearTimeout,
        setInterval: fakeSetInterval,
        clearInterval: fakeClearInterval,
        URL: { createObjectURL: () => "blob:mock", revokeObjectURL: () => {} },
        Blob: class FakeBlob { constructor(parts) { this.parts = parts; } },
        navigator: { clipboard: { writeText: async () => {} } },
        __setFetchHandler(fn) { fetchHandler = fn; },
        __flushTimeouts: flushTimeouts,
        __timeouts: timeouts,
        __FakeElement: FakeElement,
        assertDeepEqual(actual, expected, msg) {
          assert.deepStrictEqual(
            JSON.parse(JSON.stringify(actual)),
            JSON.parse(JSON.stringify(expected)),
            msg
          );
        },
      };
      sandbox.globalThis = sandbox;

      const ctx = vm.createContext(sandbox);
      getOrCreateById("m-type");
      vm.runInContext(appSource, ctx, { filename: "static/app.js" });
      return {
        ctx,
        run: (code) => vm.runInContext(`{\n${code}\n}`, ctx),
        drainMicrotasks: async () => {
          for (let i = 0; i < 30; i++) await Promise.resolve();
        },
      };
    }
    """
)


def _run_node_test(test_body: str) -> None:
    """Execute a Node.js assertion snippet against the real `static/app.js`."""
    script = (
        _NODE_HARNESS
        + "\n(async () => {\n"
        + f"  const JS_PATH = {json.dumps(str(JS_PATH))};\n"
        + test_body
        + "\n})().catch((err) => { console.error(err && err.stack ? err.stack : err); process.exit(1); });\n"
    )
    proc = subprocess.run(
        [NODE_BIN, "-e", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"Node test failed (exit {proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )


class TestFrontendStaticIntegrity(unittest.TestCase):
    """Static checks validating HTML, JS, and CSS consistency."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = HTML_PATH.read_text(encoding="utf-8")
        cls.js = JS_PATH.read_text(encoding="utf-8")
        cls.css = CSS_PATH.read_text(encoding="utf-8")

    @unittest.skipUnless(NODE_BIN, "node is not available on PATH")
    def test_app_js_node_syntax_check(self) -> None:
        proc = subprocess.run(
            [NODE_BIN, "--check", str(JS_PATH)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"node --check static/app.js failed:\n{proc.stderr}",
        )

    def test_all_dom_id_lookups_exist_in_html(self) -> None:
        raw_html_ids = re.findall(r"""\bid=["']([^"']+)["']""", self.html)
        html_ids = set(raw_html_ids)
        # No duplicate element IDs in index.html.
        self.assertEqual(
            len(raw_html_ids),
            len(html_ids),
            f"Duplicate element IDs found in templates/index.html: "
            f"{sorted({i for i in raw_html_ids if raw_html_ids.count(i) > 1})}",
        )

        # Every direct $("...") or $('...') lookup in static/app.js.
        direct_ids = set(re.findall(r"""\$\(\s*["']([^"']+)["']\s*\)""", self.js))
        self.assertGreaterEqual(len(direct_ids), 60, "Expected >= 60 DOM ID lookups in static/app.js")

        # Also check known dynamic ID arrays / data-log-copy targets.
        sections_match = re.search(r"""const\s+SECTIONS\s*=\s*\[([^\]]+)\]""", self.js)
        self.assertIsNotNone(sections_match, "SECTIONS constant not found in static/app.js")
        section_ids = set(re.findall(r"""["']([^"']+)["']""", sections_match.group(1)))

        log_copy_ids = set(re.findall(r"""data-log-copy=["']([^"']+)["']""", self.html))
        self.assertTrue(log_copy_ids, "Expected data-log-copy targets in templates/index.html")

        all_referenced = direct_ids | section_ids | log_copy_ids
        missing = sorted(all_referenced - html_ids)
        self.assertEqual(
            missing,
            [],
            f"DOM IDs referenced by frontend but missing in templates/index.html: {missing}",
        )

    def test_all_icon_references_exist_in_svg_sprite(self) -> None:
        raw_symbols = re.findall(r"""<symbol\b[^>]*\bid=["'](i-[a-z0-9-]+)["']""", self.html)
        symbols = set(raw_symbols)
        self.assertGreaterEqual(len(symbols), 20, "Expected SVG sprite symbols in templates/index.html")
        self.assertEqual(
            len(raw_symbols),
            len(symbols),
            f"Duplicate SVG symbol IDs found: "
            f"{sorted({s for s in raw_symbols if raw_symbols.count(s) > 1})}",
        )

        html_refs = set(re.findall(r"""#(i-[a-z0-9-]+)""", self.html))
        js_hash_refs = set(re.findall(r"""#(i-[a-z0-9-]+)""", self.js))
        js_btn_refs = set(re.findall(r"""makeIconBtn\(\s*["'](i-[a-z0-9-]+)["']""", self.js))

        all_refs = html_refs | js_hash_refs | js_btn_refs
        self.assertTrue(all_refs, "Expected icon references in templates/index.html and static/app.js")
        missing = sorted(all_refs - symbols)
        self.assertEqual(
            missing,
            [],
            f"Icon references missing <symbol> in templates/index.html: {missing}",
        )

    def test_no_videolabeler_or_labeler_leftovers(self) -> None:
        forbidden = re.compile(r"videolabeler|video[\s_-]?labeler|labeler", re.IGNORECASE)
        for path, content in (
            ("templates/index.html", self.html),
            ("static/app.js", self.js),
            ("static/style.css", self.css),
        ):
            hits = forbidden.findall(content)
            self.assertEqual(hits, [], f"Found leftover VideoLabeler/labeler strings in {path}: {hits}")

        self.assertIn("AutoClick", self.html)
        self.assertIn('APP_TITLE = "AutoClick"', self.js)

    def test_update_live_ui_highlights_without_rebuilding_steps_dom(self) -> None:
        body = _extract_js_function_body(self.js, "updateLiveUI")
        self.assertIn(
            "highlightActiveSteps(",
            body,
            "updateLiveUI() must highlight active steps via highlightActiveSteps()",
        )
        for forbidden_call in (
            "renderSequenceWorkspace",
            "renderRecordedSteps",
            "renderClickerPoints",
            "renderHomePresets",
            "replaceChildren",
        ):
            self.assertNotIn(
                forbidden_call,
                body,
                f"updateLiveUI() must not call {forbidden_call} during live polling "
                "(rebuilding rows mid-pause breaks Delete/Reorder clicks)",
            )

    def test_css_balanced_braces_and_required_classes(self) -> None:
        cleaned = re.sub(r"/\*.*?\*/", "", self.css, flags=re.DOTALL)
        self.assertEqual(cleaned.count("{"), cleaned.count("}"), "Unbalanced braces in static/style.css")
        for selector in (
            '[data-theme="dark"]',
            ".hidden",
            ".active-step",
            ".disabled-step",
            ".group-row",
            ".drag-grip",
            "tr.dragging",
            "tr.drag-over-top",
            "tr.drag-over-bottom",
        ):
            self.assertIn(selector, self.css, f"Expected CSS rule {selector!r} in static/style.css")


@unittest.skipUnless(NODE_BIN, "node is not available on PATH")
class TestFrontendJsLogic(unittest.TestCase):
    """Functional tests exercising real `static/app.js` functions in Node.js."""

    def test_move_item_reordering_and_bounds(self) -> None:
        _run_node_test(
            r"""
            const app = createAppSandbox({ jsPath: JS_PATH });
            await app.drainMicrotasks();
            app.run(`
              // Move down (adjacent and multi-slot)
              let list = ["a", "b", "c", "d"];
              assert.strictEqual(moveItem(list, 0, 1), true);
              assert.deepStrictEqual(list, ["b", "a", "c", "d"]);

              assert.strictEqual(moveItem(list, 0, 2), true);
              assert.deepStrictEqual(list, ["a", "c", "b", "d"]);

              // Move up
              assert.strictEqual(moveItem(list, 3, 1), true);
              assert.deepStrictEqual(list, ["a", "d", "c", "b"]);

              // Drag to top (last -> 0)
              assert.strictEqual(moveItem(list, 3, 0), true);
              assert.deepStrictEqual(list, ["b", "a", "d", "c"]);

              // Drag to bottom (0 -> last)
              assert.strictEqual(moveItem(list, 0, 3), true);
              assert.deepStrictEqual(list, ["a", "d", "c", "b"]);

              // Same-index no-op
              assert.strictEqual(moveItem(list, 2, 2), false);
              assert.deepStrictEqual(list, ["a", "d", "c", "b"]);

              // Out-of-bounds no-ops
              assert.strictEqual(moveItem(list, -1, 2), false);
              assert.strictEqual(moveItem(list, 1, -1), false);
              assert.strictEqual(moveItem(list, 4, 0), false);
              assert.strictEqual(moveItem(list, 0, 4), false);
              assert.deepStrictEqual(list, ["a", "d", "c", "b"]);

              // Empty list no-op
              const empty = [];
              assert.strictEqual(moveItem(empty, 0, 0), false);
              assert.deepStrictEqual(empty, []);
            `);
            """
        )

    def test_count_leaves_and_max_depth(self) -> None:
        _run_node_test(
            r"""
            const app = createAppSandbox({ jsPath: JS_PATH });
            await app.drainMicrotasks();
            app.run(`
              // Empty / null inputs
              assert.strictEqual(countLeaves([]), 0);
              assert.strictEqual(countLeaves(null), 0);
              assert.strictEqual(maxDepth([]), 0);
              assert.strictEqual(maxDepth(null), 0);

              // Flat steps (depth 1) with disabled step
              const flat = [
                { action: "hotkey", enabled: true },
                { action: "key", enabled: false },
                { action: "click" }, // enabled defaults to true
              ];
              assert.strictEqual(countLeaves(flat), 2);
              assert.strictEqual(maxDepth(flat), 1);

              // Depth 2: repeat group x3 with 2 enabled + 1 disabled step
              const depth2 = [
                { action: "hotkey", enabled: true },
                {
                  action: "group",
                  repeat: 3,
                  enabled: true,
                  steps: [
                    { action: "key", enabled: true },
                    { action: "click", enabled: true },
                    { action: "wait", enabled: false },
                  ],
                },
              ];
              assert.strictEqual(countLeaves(depth2), 1 + 2 * 3);
              assert.strictEqual(maxDepth(depth2), 2);

              // Disabled group contributes 0 leaves
              const disabledGroup = [
                {
                  action: "group",
                  repeat: 5,
                  enabled: false,
                  steps: [{ action: "key", enabled: true }],
                },
              ];
              assert.strictEqual(countLeaves(disabledGroup), 0);

              // Depth 3: outer group x2 -> inner group x4 (2 leaves) + 1 sibling leaf
              const depth3 = [
                {
                  action: "group",
                  repeat: 2,
                  steps: [
                    {
                      action: "group",
                      repeat: 4,
                      steps: [
                        { action: "hotkey", enabled: true },
                        { action: "type", enabled: true },
                      ],
                    },
                    { action: "wait", enabled: true },
                  ],
                },
              ];
              assert.strictEqual(countLeaves(depth3), 2 * (4 * 2 + 1));
              assert.strictEqual(maxDepth(depth3), 3);

              // Empty group at depth 1 still reports depth 2
              const emptyGroup = [{ action: "group", repeat: 2, steps: [] }];
              assert.strictEqual(countLeaves(emptyGroup), 0);
              assert.strictEqual(maxDepth(emptyGroup), 2);
            `);
            """
        )

    def test_fmt_key_formatting(self) -> None:
        _run_node_test(
            r"""
            const app = createAppSandbox({ jsPath: JS_PATH });
            await app.drainMicrotasks();
            app.run(`
              assert.strictEqual(fmtKey(""), "");
              assert.strictEqual(fmtKey(null), "");
              assert.strictEqual(fmtKey(undefined), "");
              assert.strictEqual(fmtKey("a"), "A");
              assert.strictEqual(fmtKey("z"), "Z");
              assert.strictEqual(fmtKey("ctrl"), "Ctrl");
              assert.strictEqual(fmtKey("shift"), "Shift");
              assert.strictEqual(fmtKey("enter"), "Enter");
              assert.strictEqual(fmtKey("f8"), "F8");
              assert.strictEqual(fmtKey("backspace"), "Backspace");
              assert.strictEqual(fmtKey("Ready"), "Ready");
            `);
            """
        )

    def test_highlight_active_steps_and_mid_pause_delete(self) -> None:
        _run_node_test(
            r"""
            const app = createAppSandbox({ jsPath: JS_PATH });
            await app.drainMicrotasks();
            app.run(`
              const tbody = document.getElementById("seq-steps-body");
              assert.strictEqual(tbody.children.length, 3);
              const origRows = tbody.children.slice();

              // Highlight step s2 without removing or replacing rows
              highlightActiveSteps("s2");
              assert.strictEqual(tbody.children.length, 3);
              assert.strictEqual(tbody.children[0], origRows[0]);
              assert.strictEqual(tbody.children[1], origRows[1]);
              assert.strictEqual(tbody.children[2], origRows[2]);
              assert.strictEqual(origRows[0].classList.contains("active-step"), false);
              assert.strictEqual(origRows[1].classList.contains("active-step"), true);
              assert.strictEqual(origRows[2].classList.contains("active-step"), false);

              // Move highlight to s3
              highlightActiveSteps("s3");
              assert.strictEqual(origRows[1].classList.contains("active-step"), false);
              assert.strictEqual(origRows[2].classList.contains("active-step"), true);

              // Clear highlight
              highlightActiveSteps(null);
              assert.strictEqual(origRows.some((r) => r.classList.contains("active-step")), false);

              // Regression check: repeated updateLiveUI() while paused keeps exact row DOM nodes intact
              runnerState = {
                status: "paused",
                active: true,
                step_id: "s1",
                step_label: "Hotkey Ctrl+L",
                step_depth: 1,
                iteration: 2,
                total_iterations: 5,
                infinite: false,
                steps_completed: 4,
                total_steps: 3,
                elapsed: 1.5,
                countdown_remaining: 0,
                logs: ["Paused"],
              };
              updateLiveUI();
              updateLiveUI();
              assert.strictEqual(tbody.children.length, 3);
              assert.strictEqual(tbody.children[0], origRows[0]);
              assert.strictEqual(tbody.children[1], origRows[1]);
              assert.strictEqual(tbody.children[2], origRows[2]);
              assert.strictEqual(origRows[0].classList.contains("active-step"), true);
              assert.strictEqual(origRows[1].classList.contains("active-step"), false);

              // Clicking the Delete button on row 1 (s2) while paused deletes s2 cleanly
              const row1Buttons = origRows[1].querySelectorAll("button");
              const delBtn = row1Buttons.find((b) => b.title === "Delete step");
              assert.ok(delBtn, "Expected Delete step button on sequence row");
              delBtn.click();

              assertDeepEqual(
                activePreset().steps.map((s) => s.id),
                ["s1", "s3"]
              );
              assert.strictEqual(tbody.children.length, 2);
            `);
            """
        )

    def test_stale_save_seq_and_rapid_reorder_delete_preserves_edits(self) -> None:
        _run_node_test(
            r"""
            const initialState = {
              presets: [
                {
                  id: "p1",
                  name: "Rapid Edit Test",
                  start_delay: 5,
                  iterations: 5,
                  infinite: false,
                  steps: [
                    { id: "s1", action: "hotkey", keys: ["ctrl", "l"], hold_ms: 0, delay: 0.1, enabled: true },
                    { id: "s2", action: "hotkey", keys: ["ctrl", "c"], hold_ms: 0, delay: 0.1, enabled: true },
                    { id: "s3", action: "hotkey", keys: ["ctrl", "v"], hold_ms: 0, delay: 0.1, enabled: true },
                    { id: "s4", action: "key", key: "enter", presses: 1, hold_ms: 0, delay: 0.1, enabled: true },
                  ],
                },
              ],
              active_preset_id: "p1",
              settings: { stop_key: "f8", record_stop_key: "f9", failsafe: true, minimize_on_start: false, speed: 1.0 },
              runner: { status: "idle", active: false, iteration: 0, total_iterations: 0, steps_completed: 0, total_steps: 0, elapsed: 0, countdown_remaining: 0, logs: [] },
              recorder: { status: "idle", active: false, step_count: 0, steps: [], elapsed: 0, cooloff_remaining: 0, logs: [] },
            };

            const app = createAppSandbox({ jsPath: JS_PATH, initialState });
            await app.drainMicrotasks();

            const saveRequests = [];
            app.ctx.__setFetchHandler((url, opts) => {
              if (url === "/api/presets/save") {
                const body = JSON.parse(opts.body);
                return new Promise((resolve) => {
                  saveRequests.push({ body, resolve });
                });
              }
              return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
            });

            // Edit 1: Move s1 down -> [s2, s1, s3, s4] and fire debounced save #1
            app.run(`
              const p = activePreset();
              assert.strictEqual(moveItem(p.steps, 0, 1), true);
              scheduleSavePreset();
              __flushTimeouts();
            `);
            await app.drainMicrotasks();
            assert.strictEqual(saveRequests.length, 1);
            assert.deepStrictEqual(
              saveRequests[0].body.preset.steps.map((s) => s.id),
              ["s2", "s1", "s3", "s4"]
            );

            // Edit 2 (while save #1 is STILL in-flight): delete s3 -> [s2, s1, s4] and fire save #2
            app.run(`
              const p = activePreset();
              p.steps.splice(2, 1);
              scheduleSavePreset();
              __flushTimeouts();
            `);
            await app.drainMicrotasks();
            assert.strictEqual(saveRequests.length, 2);
            assert.deepStrictEqual(
              saveRequests[1].body.preset.steps.map((s) => s.id),
              ["s2", "s1", "s4"]
            );

            // Resolve stale save #1 FIRST: must be ignored so deleted s3 does NOT reappear!
            saveRequests[0].resolve({
              ok: true,
              status: 200,
              json: async () => ({
                ok: true,
                presets: [JSON.parse(JSON.stringify(saveRequests[0].body.preset))],
                active_preset_id: "p1",
              }),
            });
            await app.drainMicrotasks();

            app.run(`
              assertDeepEqual(
                activePreset().steps.map((s) => s.id),
                ["s2", "s1", "s4"],
                "Stale save response must NOT overwrite newer local edits"
              );
            `);

            // Resolve latest save #2: accepted cleanly
            saveRequests[1].resolve({
              ok: true,
              status: 200,
              json: async () => ({
                ok: true,
                presets: [JSON.parse(JSON.stringify(saveRequests[1].body.preset))],
                active_preset_id: "p1",
              }),
            });
            await app.drainMicrotasks();

            app.run(`
              assertDeepEqual(
                activePreset().steps.map((s) => s.id),
                ["s2", "s1", "s4"]
              );
            `);

            // Rapid debounce coalescing: reorder + delete within debounce window sends only 1 request
            app.run(`
              const p = activePreset();
              assert.strictEqual(moveItem(p.steps, 2, 0), true); // [s4, s2, s1]
              scheduleSavePreset();
              p.steps.splice(1, 1); // delete s2 -> [s4, s1]
              scheduleSavePreset();
              __flushTimeouts();
            `);
            await app.drainMicrotasks();
            assert.strictEqual(saveRequests.length, 3, "Debounced rapid edits should coalesce into 1 save request");
            assert.deepStrictEqual(
              saveRequests[2].body.preset.steps.map((s) => s.id),
              ["s4", "s1"]
            );
            """
        )

    def test_drag_and_drop_row_reordering(self) -> None:
        _run_node_test(
            r"""
            const app = createAppSandbox({ jsPath: JS_PATH });
            await app.drainMicrotasks();
            app.run(`
              const tbody = document.getElementById("seq-steps-body");
              let rows = tbody.children;
              assertDeepEqual(activePreset().steps.map((s) => s.id), ["s1", "s2", "s3"]);

              // Dragging from a button inside the row must be cancelled so clicks work
              const btnTarget = new __FakeElement("button");
              const blocked = rows[0].dispatchEvent({ type: "dragstart", target: btnTarget });
              assert.strictEqual(blocked.defaultPrevented, true);
              assert.strictEqual(dragState, null);

              // Drag row 0 (s1) and drop onto bottom half of row 2 (s3) -> [s2, s3, s1]
              const dt = { setData() {}, effectAllowed: "", dropEffect: "" };
              rows[0].dispatchEvent({ type: "dragstart", target: rows[0], dataTransfer: dt });
              assert.ok(dragState && dragState.fromIndex === 0);
              rows[2].dispatchEvent({ type: "dragover", clientY: 35, dataTransfer: dt });
              assert.strictEqual(rows[2].classList.contains("drag-over-bottom"), true);
              rows[2].dispatchEvent({ type: "drop", clientY: 35, dataTransfer: dt });
              assertDeepEqual(activePreset().steps.map((s) => s.id), ["s2", "s3", "s1"]);

              // Drag row 2 (s1) back to top half of row 0 (s2) -> [s1, s2, s3]
              rows = tbody.children;
              rows[2].dispatchEvent({ type: "dragstart", target: rows[2], dataTransfer: dt });
              rows[0].dispatchEvent({ type: "dragover", clientY: 5, dataTransfer: dt });
              assert.strictEqual(rows[0].classList.contains("drag-over-top"), true);
              rows[0].dispatchEvent({ type: "drop", clientY: 5, dataTransfer: dt });
              assertDeepEqual(activePreset().steps.map((s) => s.id), ["s1", "s2", "s3"]);
            `);
            """
        )

    def test_consecutive_drags_across_completed_saves(self) -> None:
        _run_node_test(
            r"""
            const initialState = {
              presets: [
                {
                  id: "p1",
                  name: "Multi-Drag Test",
                  start_delay: 5,
                  iterations: 3,
                  infinite: false,
                  steps: [
                    { id: "s1", action: "hotkey", keys: ["ctrl", "l"], hold_ms: 0, delay: 0.1, enabled: true },
                    { id: "s2", action: "hotkey", keys: ["ctrl", "c"], hold_ms: 0, delay: 0.1, enabled: true },
                    {
                      id: "g1",
                      action: "group",
                      name: "Inner Group",
                      repeat: 2,
                      delay: 0.1,
                      enabled: true,
                      steps: [
                        { id: "g1_a", action: "key", key: "a", presses: 1, hold_ms: 0, delay: 0.1, enabled: true },
                        { id: "g1_b", action: "key", key: "b", presses: 1, hold_ms: 0, delay: 0.1, enabled: true },
                        { id: "g1_c", action: "key", key: "c", presses: 1, hold_ms: 0, delay: 0.1, enabled: true },
                      ],
                    },
                    { id: "s3", action: "key", key: "enter", presses: 1, hold_ms: 0, delay: 0.1, enabled: true },
                  ],
                },
              ],
              active_preset_id: "p1",
              settings: { stop_key: "f8", record_stop_key: "f9", failsafe: true, minimize_on_start: false, speed: 1.0 },
              runner: { status: "idle", active: false, iteration: 0, total_iterations: 0, steps_completed: 0, total_steps: 0, elapsed: 0, countdown_remaining: 0, logs: [] },
              recorder: { status: "idle", active: false, step_count: 0, steps: [], elapsed: 0, cooloff_remaining: 0, logs: [] },
            };

            const app = createAppSandbox({ jsPath: JS_PATH, initialState });
            await app.drainMicrotasks();

            let saveCount = 0;
            app.ctx.__setFetchHandler(async (url, opts) => {
              if (url === "/api/presets/save") {
                saveCount += 1;
                const body = JSON.parse(opts.body);
                return {
                  ok: true,
                  status: 200,
                  json: async () => ({
                    ok: true,
                    preset: JSON.parse(JSON.stringify(body.preset)),
                    presets: [JSON.parse(JSON.stringify(body.preset))],
                    active_preset_id: body.preset.id,
                  }),
                };
              }
              return { ok: true, status: 200, json: async () => ({ ok: true }) };
            });

            const dragRow = (fromStepId, toStepId, clientY) => {
              app.run(`
                const rows = document.getElementById("seq-steps-body").children;
                const fromRow = rows.find((r) => r.dataset.stepId === ${JSON.stringify(fromStepId)});
                const toRow = rows.find((r) => r.dataset.stepId === ${JSON.stringify(toStepId)});
                assert.ok(fromRow && toRow, "Expected rows for drag");
                const dt = { setData() {}, effectAllowed: "", dropEffect: "" };
                fromRow.dispatchEvent({ type: "dragstart", target: fromRow, dataTransfer: dt });
                toRow.dispatchEvent({ type: "dragenter", dataTransfer: dt });
                toRow.dispatchEvent({ type: "dragover", clientY: ${clientY}, dataTransfer: dt });
                toRow.dispatchEvent({ type: "drop", clientY: ${clientY}, dataTransfer: dt });
              `);
            };

            // Drag #1 (root): s1 -> bottom of s3 => [s2, g1, s3, s1]
            let prevRef = app.run(`presets`);
            dragRow("s1", "s3", 35);
            app.run(`__flushTimeouts();`);
            await app.drainMicrotasks();
            assert.strictEqual(saveCount, 1);
            assert.notStrictEqual(app.run(`presets`), prevRef, "Save should replace presets with fresh objects");
            app.run(`
              assertDeepEqual(activePreset().steps.map((s) => s.id), ["s2", "g1", "s3", "s1"]);
              assertDeepEqual(
                document.getElementById("seq-steps-body").children.map((r) => r.dataset.stepId),
                ["s2", "g1", "g1_a", "g1_b", "g1_c", "s3", "s1"]
              );
            `);

            // Drag #2 (root, after save #1 replaced presets): s2 -> bottom of s3 => [g1, s3, s2, s1]
            prevRef = app.run(`presets`);
            dragRow("s2", "s3", 35);
            app.run(`__flushTimeouts();`);
            await app.drainMicrotasks();
            assert.strictEqual(saveCount, 2);
            assert.notStrictEqual(app.run(`presets`), prevRef);
            app.run(`
              assertDeepEqual(activePreset().steps.map((s) => s.id), ["g1", "s3", "s2", "s1"]);
              assertDeepEqual(
                document.getElementById("seq-steps-body").children.map((r) => r.dataset.stepId),
                ["g1", "g1_a", "g1_b", "g1_c", "s3", "s2", "s1"]
              );
            `);

            // Drag #3 (root, after save #2 replaced presets): s1 -> top of g1 => [s1, g1, s3, s2]
            dragRow("s1", "g1", 5);
            app.run(`__flushTimeouts();`);
            await app.drainMicrotasks();
            assert.strictEqual(saveCount, 3);
            app.run(`
              assertDeepEqual(activePreset().steps.map((s) => s.id), ["s1", "g1", "s3", "s2"]);
            `);

            // Cross-group drag (root s1 onto nested g1_a) must be ignored
            dragRow("s1", "g1_a", 5);
            app.run(`dragState = null;`);
            app.run(`
              assertDeepEqual(activePreset().steps.map((s) => s.id), ["s1", "g1", "s3", "s2"]);
            `);

            // Consecutive drags inside nested Repeat Group grp-g1 across completed saves
            // Nested Drag #1: g1_a -> bottom of g1_c => [g1_b, g1_c, g1_a]
            dragRow("g1_a", "g1_c", 35);
            app.run(`__flushTimeouts();`);
            await app.drainMicrotasks();
            assert.strictEqual(saveCount, 4);
            app.run(`
              assertDeepEqual(
                activePreset().steps.find((s) => s.id === "g1").steps.map((s) => s.id),
                ["g1_b", "g1_c", "g1_a"]
              );
            `);

            // Nested Drag #2 (after save #4 replaced presets): g1_b -> bottom of g1_a => [g1_c, g1_a, g1_b]
            dragRow("g1_b", "g1_a", 35);
            app.run(`__flushTimeouts();`);
            await app.drainMicrotasks();
            assert.strictEqual(saveCount, 5);
            app.run(`
              assertDeepEqual(
                activePreset().steps.find((s) => s.id === "g1").steps.map((s) => s.id),
                ["g1_c", "g1_a", "g1_b"]
              );
            `);
            """
        )

    def test_save_completing_mid_drag_updates_live_list(self) -> None:
        _run_node_test(
            r"""
            const initialState = {
              presets: [
                {
                  id: "p1",
                  name: "Mid-Drag Save Test",
                  start_delay: 5,
                  iterations: 3,
                  infinite: false,
                  steps: [
                    { id: "s1", action: "hotkey", keys: ["ctrl", "l"], hold_ms: 0, delay: 0.1, enabled: true },
                    { id: "s2", action: "hotkey", keys: ["ctrl", "c"], hold_ms: 0, delay: 0.1, enabled: true },
                    { id: "s3", action: "hotkey", keys: ["ctrl", "v"], hold_ms: 0, delay: 0.1, enabled: true },
                    { id: "s4", action: "key", key: "enter", presses: 1, hold_ms: 0, delay: 0.1, enabled: true },
                  ],
                },
              ],
              active_preset_id: "p1",
              settings: { stop_key: "f8", record_stop_key: "f9", failsafe: true, minimize_on_start: false, speed: 1.0 },
              runner: { status: "idle", active: false, iteration: 0, total_iterations: 0, steps_completed: 0, total_steps: 0, elapsed: 0, countdown_remaining: 0, logs: [] },
              recorder: { status: "idle", active: false, step_count: 0, steps: [], elapsed: 0, cooloff_remaining: 0, logs: [] },
            };

            const app = createAppSandbox({ jsPath: JS_PATH, initialState });
            await app.drainMicrotasks();

            const saveRequests = [];
            app.ctx.__setFetchHandler((url, opts) => {
              if (url === "/api/presets/save") {
                const body = JSON.parse(opts.body);
                return new Promise((resolve) => { saveRequests.push({ body, resolve }); });
              }
              return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
            });

            // Drag #1: s1 -> bottom of s2 => [s2, s1, s3, s4], flush timeout so save #1 is in-flight
            app.run(`
              const rows = document.getElementById("seq-steps-body").children;
              const dt = { setData() {}, effectAllowed: "", dropEffect: "" };
              rows[0].dispatchEvent({ type: "dragstart", target: rows[0], dataTransfer: dt });
              rows[1].dispatchEvent({ type: "dragover", clientY: 35, dataTransfer: dt });
              rows[1].dispatchEvent({ type: "drop", clientY: 35, dataTransfer: dt });
              assertDeepEqual(activePreset().steps.map((s) => s.id), ["s2", "s1", "s3", "s4"]);
              __flushTimeouts();
            `);
            await app.drainMicrotasks();
            assert.strictEqual(saveRequests.length, 1);

            // Start Drag #2 (dragstart on s4 at index 3) BEFORE save #1 resolves
            app.run(`
              const rows = document.getElementById("seq-steps-body").children;
              const dt = { setData() {}, effectAllowed: "", dropEffect: "" };
              rows[3].dispatchEvent({ type: "dragstart", target: rows[3], dataTransfer: dt });
              assert.ok(dragState && dragState.fromIndex === 3);
            `);

            // Resolve save #1 while Drag #2 is mid-drag (replaces `presets` with fresh cloned array)
            saveRequests[0].resolve({
              ok: true,
              status: 200,
              json: async () => ({
                ok: true,
                presets: [JSON.parse(JSON.stringify(saveRequests[0].body.preset))],
                active_preset_id: "p1",
              }),
            });
            await app.drainMicrotasks();

            // Finish Drag #2 onto top of row 0 (s2) -> must mutate the NEW live preset array!
            app.run(`
              const rows = document.getElementById("seq-steps-body").children;
              const dt = { setData() {}, effectAllowed: "", dropEffect: "" };
              rows[0].dispatchEvent({ type: "dragover", clientY: 5, dataTransfer: dt });
              assert.strictEqual(rows[0].classList.contains("drag-over-top"), true);
              rows[0].dispatchEvent({ type: "drop", clientY: 5, dataTransfer: dt });
              assertDeepEqual(
                activePreset().steps.map((s) => s.id),
                ["s4", "s2", "s1", "s3"]
              );
            `);
            """
        )

    def test_post_save_row_controls_and_modal_edits_without_reentering(self) -> None:
        _run_node_test(
            r"""
            const initialState = {
              presets: [
                {
                  id: "p1",
                  name: "Row Controls Test",
                  start_delay: 5,
                  iterations: 3,
                  infinite: false,
                  steps: [
                    { id: "s1", action: "hotkey", keys: ["ctrl", "l"], hold_ms: 0, delay: 0.1, enabled: true, note: "" },
                    { id: "s2", action: "key", key: "enter", presses: 1, hold_ms: 0, delay: 0.1, enabled: true, note: "" },
                    {
                      id: "g1",
                      action: "group",
                      name: "Loop",
                      repeat: 2,
                      delay: 0.1,
                      enabled: true,
                      note: "",
                      steps: [
                        { id: "g1_a", action: "wait", seconds: 0.5, enabled: true, note: "" },
                      ],
                    },
                  ],
                },
              ],
              active_preset_id: "p1",
              settings: { stop_key: "f8", record_stop_key: "f9", failsafe: true, minimize_on_start: false, speed: 1.0 },
              runner: { status: "idle", active: false, iteration: 0, total_iterations: 0, steps_completed: 0, total_steps: 0, elapsed: 0, countdown_remaining: 0, logs: [] },
              recorder: { status: "idle", active: false, step_count: 0, steps: [], elapsed: 0, cooloff_remaining: 0, logs: [] },
            };

            const app = createAppSandbox({ jsPath: JS_PATH, initialState });
            await app.drainMicrotasks();

            app.ctx.__setFetchHandler(async (url, opts) => {
              if (url === "/api/presets/save") {
                const body = JSON.parse(opts.body);
                return {
                  ok: true,
                  status: 200,
                  json: async () => ({
                    ok: true,
                    preset: JSON.parse(JSON.stringify(body.preset)),
                    presets: [JSON.parse(JSON.stringify(body.preset))],
                    active_preset_id: body.preset.id,
                  }),
                };
              }
              return { ok: true, status: 200, json: async () => ({ ok: true }) };
            });

            // Trigger an initial save so `presets` is replaced with fresh deserialized objects
            app.run(`scheduleSavePreset(); __flushTimeouts();`);
            await app.drainMicrotasks();

            // 1. Toggle checkbox on s1 (row 0) after save replaced `presets`
            app.run(`
              const row0 = document.getElementById("seq-steps-body").children[0];
              const chk = row0.querySelector("input");
              chk.checked = false;
              chk.dispatchEvent({ type: "change" });
              assert.strictEqual(activePreset().steps[0].enabled, false);
              __flushTimeouts();
            `);
            await app.drainMicrotasks();

            // 2. Click Move down on row 0 (s1) after save -> [s2, s1, g1]
            app.run(`
              const row0 = document.getElementById("seq-steps-body").children[0];
              const downBtn = row0.querySelectorAll("button").find((b) => b.title === "Move down");
              downBtn.click();
              assertDeepEqual(activePreset().steps.map((s) => s.id), ["s2", "s1", "g1"]);
              __flushTimeouts();
            `);
            await app.drainMicrotasks();

            // 3. Click Edit on row 0 (s2), let a background save replace `presets` while modal is open, then commit
            app.run(`
              const row0 = document.getElementById("seq-steps-body").children[0];
              const editBtn = row0.querySelectorAll("button").find((b) => b.title === "Edit step");
              editBtn.click();
              scheduleSavePreset();
              __flushTimeouts();
            `);
            await app.drainMicrotasks();
            app.run(`
              $("m-note").value = "Updated note";
              commitStepModal();
              assert.strictEqual(activePreset().steps[0].note, "Updated note");
              __flushTimeouts();
            `);
            await app.drainMicrotasks();

            // 4. Click "+ Sub" on g1 (row 2) after save, commit new sub-step
            app.run(`
              const g1Row = document.getElementById("seq-steps-body").children.find((r) => r.dataset.stepId === "g1");
              const addSubBtn = g1Row.querySelectorAll("button").find((b) => b.textContent === "+ Sub");
              addSubBtn.click();
              $("m-keys").value = "ctrl+a";
              commitStepModal();
              assert.strictEqual(activePreset().steps[2].steps.length, 2);
              assertDeepEqual(activePreset().steps[2].steps[1].keys, ["ctrl", "a"]);
              __flushTimeouts();
            `);
            await app.drainMicrotasks();

            // 5. Click Delete on s1 (row 1) after save -> [s2, g1]
            app.run(`
              const s1Row = document.getElementById("seq-steps-body").children.find((r) => r.dataset.stepId === "s1");
              const delBtn = s1Row.querySelectorAll("button").find((b) => b.title === "Delete step");
              delBtn.click();
              assertDeepEqual(activePreset().steps.map((s) => s.id), ["s2", "g1"]);
            `);
            """
        )

    def test_stop_recording_button_syncs_steps_and_enables_actions(self) -> None:
        _run_node_test(
            r"""
            const app = createAppSandbox({ jsPath: JS_PATH });
            await app.drainMicrotasks();

            const captured = [
              { id: "r1", action: "click", x: -800, y: 400, button: "left", clicks: 1, hold_ms: 0, delay: 0.2, enabled: true, note: "" },
              { id: "r2", action: "click", x: 2800, y: 600, button: "left", clicks: 1, hold_ms: 0, delay: 0.1, enabled: true, note: "" },
            ];

            app.ctx.__setFetchHandler(async (url) => {
              if (url === "/api/settings") {
                return {
                  ok: true,
                  status: 200,
                  json: async () => ({
                    ok: true,
                    settings: { stop_key: "f8", record_stop_key: "f9", failsafe: true, minimize_on_start: false, speed: 1.0 },
                  }),
                };
              }
              if (url === "/api/record/start") {
                return {
                  ok: true,
                  status: 200,
                  json: async () => ({
                    ok: true,
                    recorder: {
                      status: "recording",
                      active: true,
                      mode: "both",
                      stop_key: "f9",
                      cooloff_remaining: 0,
                      elapsed: 0.1,
                      step_count: 0,
                      steps: [],
                      error: "",
                      logs: ["Recording started!"],
                    },
                  }),
                };
              }
              if (url === "/api/record/stop") {
                return {
                  ok: true,
                  status: 200,
                  json: async () => ({
                    ok: true,
                    recorder: {
                      status: "done",
                      active: false,
                      mode: "both",
                      stop_key: "f9",
                      cooloff_remaining: 0,
                      elapsed: 0,
                      step_count: captured.length,
                      steps: captured,
                      error: "",
                      logs: ["Recording stopped."],
                    },
                  }),
                };
              }
              return { ok: true, status: 200, json: async () => ({ ok: true }) };
            });

            // Start recording -> active: true
            app.run(`$("rec-start").click();`);
            await app.drainMicrotasks();
            app.run(`
              assert.strictEqual(recorderState.active, true);
              assert.strictEqual(recordedSteps.length, 0);
            `);

            // Click Stop Rec -> immediately syncs recordedSteps & enables Replay/Save/Edit/Clear
            app.run(`$("rec-stop").click();`);
            await app.drainMicrotasks();
            app.run(`
              assert.strictEqual(recorderState.active, false);
              assert.strictEqual(recordedSteps.length, 2);
              assert.strictEqual(recordedSteps[0].x, -800);
              assert.strictEqual(recordedSteps[1].x, 2800);
              assert.strictEqual($("rec-steps-body").children.length, 2);
              assert.strictEqual($("rec-replay").disabled, false);
              assert.strictEqual($("rec-save-btn").disabled, false);
              assert.strictEqual($("rec-open-builder").disabled, false);
              assert.strictEqual($("rec-clear").disabled, false);
            `);
            """
        )

    def test_update_banner_shows_when_new_version_available(self) -> None:
        _run_node_test(
            r"""
            const defaultState = {
              presets: [
                {
                  id: "p1",
                  name: "Test Preset",
                  start_delay: 5,
                  iterations: 3,
                  infinite: false,
                  steps: [{ id: "s1", action: "wait", seconds: 0.5, enabled: true, note: "" }],
                },
              ],
              active_preset_id: "p1",
              settings: { stop_key: "f8", record_stop_key: "f9", failsafe: true, minimize_on_start: false, speed: 1.0 },
              runner: { status: "idle", active: false, iteration: 0, total_iterations: 0, steps_completed: 0, total_steps: 0, elapsed: 0, countdown_remaining: 0, logs: [] },
              recorder: { status: "idle", active: false, step_count: 0, steps: [], elapsed: 0, cooloff_remaining: 0, logs: [] },
            };

            const app = createAppSandbox({
              jsPath: JS_PATH,
              initialState: defaultState,
              fetchHandler: async (url) => {
                if (url === "/api/state") {
                  return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(defaultState)) };
                }
                if (url.startsWith("/api/update")) {
                  return {
                    ok: true,
                    status: 200,
                    json: async () => ({
                      ok: true,
                      available: true,
                      current: "1.0.0",
                      latest: "1.1.0",
                      url: "https://github.com/iqrarwaqas/AutoClick/releases/tag/v1.1.0",
                      can_install: true,
                    }),
                  };
                }
                return { ok: true, status: 200, json: async () => ({ ok: true }) };
              },
            });
            await app.drainMicrotasks();

            app.run(`
              assert.strictEqual($("update-banner").classList.contains("hidden"), false);
              assert.ok($("update-text").textContent.includes("New update available"));
              assert.ok($("update-text").textContent.includes("1.1.0"));
              assert.strictEqual($("update-install").classList.contains("hidden"), false);
              assert.strictEqual($("menu-dot").classList.contains("hidden"), false);
              assert.ok($("home-update-state").textContent.includes("1.1.0 available"));

              $("update-dismiss").click();
              assert.strictEqual($("update-banner").classList.contains("hidden"), true);
              assert.strictEqual(sessionStorage.getItem("updateDismissed"), "1.1.0");
            `);
            """
        )

    def test_build_clicker_preset_modes_and_hold_depth(self) -> None:
        _run_node_test(
            r"""
            const app = createAppSandbox({ jsPath: JS_PATH });
            await app.drainMicrotasks();
            app.run(`
              $("clk-button").value = "right";
              $("clk-clicks").value = "2";
              $("clk-hold").value = "120";
              $("clk-interval").value = "0.25";
              $("clk-cooloff").value = "3";
              $("clk-iterations").value = "10";
              $("clk-infinite").checked = false;

              // Cursor mode
              clickerTargetMode = "cursor";
              let preset = buildClickerPreset();
              assert.strictEqual(preset.steps.length, 1);
              assert.strictEqual(preset.steps[0].x, null);
              assert.strictEqual(preset.steps[0].y, null);
              assert.strictEqual(preset.steps[0].button, "right");
              assert.strictEqual(preset.steps[0].clicks, 2);
              assert.strictEqual(preset.steps[0].hold_ms, 120);
              assert.strictEqual(preset.steps[0].delay, 0.25);

              // Single point mode
              clickerTargetMode = "single";
              $("clk-x").value = "400";
              $("clk-y").value = "250";
              preset = buildClickerPreset();
              assert.strictEqual(preset.steps[0].x, 400);
              assert.strictEqual(preset.steps[0].y, 250);

              // Multi-point mode throws when empty, builds ordered steps when populated
              clickerTargetMode = "multi";
              clickerPoints = [];
              assert.throws(() => buildClickerPreset(), /Pick at least one screen point/);

              clickerPoints = [{ x: 10, y: 20 }, { x: 300, y: 400 }];
              preset = buildClickerPreset();
              assert.strictEqual(preset.steps.length, 2);
              assertDeepEqual(
                preset.steps.map((s) => [s.x, s.y, s.hold_ms]),
                [[10, 20, 120], [300, 400, 120]]
              );
            `);
            """
        )


if __name__ == "__main__":
    unittest.main()
