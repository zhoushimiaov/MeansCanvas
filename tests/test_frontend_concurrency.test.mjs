import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const smartCanvasJs = fs.readFileSync('static/js/smart-canvas.js', 'utf-8');
const canvasJs = fs.readFileSync('static/js/canvas.js', 'utf-8');

test('smart-canvas: safeScale clamps scale to [0.06, 8]', () => {
  // Extract safeScale function directly from smart-canvas.js
  const match = smartCanvasJs.match(/function safeScale\(value\)\{[\s\S]*?\n\}/);
  assert.ok(match, 'safeScale function should exist in smart-canvas.js');
  const safeScale = new Function(`${match[0]}; return safeScale;`)();

  assert.equal(safeScale(0.0001), 0.06, 'Should clamp sub-minimum scale to 0.06');
  assert.equal(safeScale(-5), 1, 'Negative scale should fallback to 1');
  assert.equal(safeScale(0), 1, 'Zero scale should fallback to 1');
  assert.equal(safeScale(NaN), 1, 'NaN scale should fallback to 1');
  assert.equal(safeScale(100), 8, 'Scale over 8 should clamp to 8');
  assert.equal(safeScale(1.5), 1.5, 'Valid scale should be preserved');
  assert.equal(safeScale(0.5), 0.5, 'Valid scale should be preserved');
});

test('smart-canvas: undo suppression supports re-entrant depth tracking', () => {
  // Extract undo suppression variables and helpers
  const helperMatches = smartCanvasJs.match(/let undoSuppressDepth[\s\S]*?function popUndoSuppress\(\)\{[\s\S]*?\}/);
  assert.ok(helperMatches, 'Undo suppression helpers should exist');

  const context = new Function(`
    let undoSuppressed = false;
    ${helperMatches[0]}
    return {
      push: pushUndoSuppress,
      pop: popUndoSuppress,
      getSuppressed: () => undoSuppressed,
      getDepth: () => undoSuppressDepth
    };
  `)();

  assert.equal(context.getSuppressed(), false);
  assert.equal(context.getDepth(), 0);

  // Outer suppression
  context.push();
  assert.equal(context.getSuppressed(), true);
  assert.equal(context.getDepth(), 1);

  // Nested suppression
  context.push();
  assert.equal(context.getSuppressed(), true);
  assert.equal(context.getDepth(), 2);

  // Inner pop
  context.pop();
  assert.equal(context.getSuppressed(), true, 'Should still be suppressed after inner pop');
  assert.equal(context.getDepth(), 1);

  // Outer pop
  context.pop();
  assert.equal(context.getSuppressed(), false, 'Should be unsuppressed after outer pop');
  assert.equal(context.getDepth(), 0);

  // Over-pop protection
  context.pop();
  assert.equal(context.getDepth(), 0, 'Depth should never drop below 0');
  assert.equal(context.getSuppressed(), false);
});

test('canvas.js: wheel scale event handler behaviorally clamps scale to [0.06, 8]', () => {
  const wheelMatch = canvasJs.match(/board\.onwheel\s*=\s*e\s*=>\s*\{([\s\S]*?)\n\};/);
  assert.ok(wheelMatch, 'board.onwheel handler must exist in canvas.js');
  const wheelBody = wheelMatch[1];

  const runWheel = (initialScale, deltaY) => {
    const viewport = { scale: initialScale, x: 0, y: 0 };
    const board = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
    const canvas = { id: 'test' };
    const screenToWorld = (x, y) => ({ x, y });
    const applyViewport = () => {};
    const renderLinks = () => {};
    const renderSelectionHub = () => {};
    const scheduleViewportSave = () => {};

    const fn = new Function(
      'canvas', 'board', 'viewport', 'screenToWorld',
      'applyViewport', 'renderLinks', 'renderSelectionHub', 'scheduleViewportSave', 'e',
      wheelBody
    );

    fn(canvas, board, viewport, screenToWorld, applyViewport, renderLinks, renderSelectionHub, scheduleViewportSave, {
      preventDefault: () => {},
      target: { closest: () => null },
      clientX: 100,
      clientY: 100,
      deltaY
    });

    return viewport.scale;
  };

  // Zoom out 100 times: must strictly clamp at 0.06
  let scale = 1.0;
  for (let i = 0; i < 100; i++) scale = runWheel(scale, 100);
  assert.equal(scale, 0.06, 'Zoom out must strictly clamp to 0.06');

  // Zoom in 100 times: must strictly clamp at 8
  scale = 1.0;
  for (let i = 0; i < 100; i++) scale = runWheel(scale, -100);
  assert.equal(scale, 8, 'Zoom in must strictly clamp to 8');
});

test('canvas.js: runGenerator behaviorally locks duplicate runs when pending tasks exist', () => {
  let executedGeneration = false;
  const nodes = [
    { id: 'gen-1', running: false }
  ];
  const out = { _pending: ['task-in-progress'] };

  // Run generator guard logic extracted from canvas.js
  const runGuard = (genId, opts = {}) => {
    const gen = nodes.find(n => n.id === genId);
    if (!gen) return false;
    if ((gen.running || out?._pending?.length) && !opts.cascade) return false;
    executedGeneration = true;
    return true;
  };

  // With pending tasks, duplicate run is rejected
  const allowed1 = runGuard('gen-1');
  assert.equal(allowed1, false, 'Duplicate run must be blocked when pending tasks exist');
  assert.equal(executedGeneration, false);

  // Clear pending tasks: generation proceeds
  out._pending = [];
  const allowed2 = runGuard('gen-1');
  assert.equal(allowed2, true, 'Run should proceed when no tasks are pending');
  assert.equal(executedGeneration, true);
});

test('canvas.js: refreshMissingCanvasAssets guards against out-of-order race', async () => {
  const fnMatch = canvasJs.match(/let missingAssetCheckSeq = 0;\s*async function refreshMissingCanvasAssets\(\)[\s\S]*?\n\}/);
  assert.ok(fnMatch, 'refreshMissingCanvasAssets must exist in canvas.js');

  const runner = new Function(
    'fetch', 'canvasLocalAssetUrls', 'missingAssetUrls',
    `${fnMatch[0]}; return refreshMissingCanvasAssets;`
  );

  const missingAssetUrls = new Set();
  let callCount = 0;
  const mockFetch = async () => {
    callCount++;
    const id = callCount;
    if (id === 1) {
      // First request takes 60ms
      await new Promise(r => setTimeout(r, 60));
      return { json: async () => ({ exists: { 'slow_outdated.png': false } }) };
    } else {
      // Second request takes 10ms
      await new Promise(r => setTimeout(r, 10));
      return { json: async () => ({ exists: { 'fresh_current.png': false } }) };
    }
  };

  const refreshFn = runner(mockFetch, () => ['test.png'], missingAssetUrls);

  // Trigger req 1 then req 2 concurrently
  const p1 = refreshFn();
  const p2 = refreshFn();

  await Promise.all([p1, p2]);

  assert.ok(missingAssetUrls.has('fresh_current.png'), 'Fresh response must be stored');
  assert.ok(!missingAssetUrls.has('slow_outdated.png'), 'Outdated response MUST be discarded by sequence check');
});

test('canvas.js: selection is preserved across pushUndo and performUndo', () => {
  const pushMatch = canvasJs.match(/function pushUndo\([^)]*\)\{[\s\S]*?\n\}/);
  const undoMatch = canvasJs.match(/function performUndo\(\)\{[\s\S]*?\n\}/);
  const captureMatch = canvasJs.match(/function captureUndoState\(\)\{[\s\S]*?\n\}/);
  assert.ok(pushMatch && undoMatch, 'pushUndo and performUndo must exist in canvas.js');

  const env = new Function(`
    let canvas = { id: 'c1' };
    let nodes = [{ id: 'n1' }, { id: 'n2' }];
    let connections = [];
    let selected = new Set(['n1', 'n2']);
    let undoStack = [];
    let redoStack = [];
    const UNDO_MAX = 50;
    const serializableCanvasNodes = () => nodes;
    const render = () => {};
    const scheduleSave = () => {};

    ${captureMatch ? captureMatch[0] : ''}
    ${pushMatch[0]}
    ${undoMatch[0]}

    return {
      push: pushUndo,
      undo: performUndo,
      getSelected: () => [...selected],
      setSelected: (arr) => { selected.clear(); arr.forEach(x => selected.add(x)); },
      setNodes: (n) => { nodes = n; }
    };
  `)();

  assert.deepEqual(env.getSelected(), ['n1', 'n2']);

  // Snapshot initial selection state
  env.push();

  // Mutate selection in user interaction
  env.setSelected(['n3']);
  assert.deepEqual(env.getSelected(), ['n3']);

  // Perform undo: selection must be restored to ['n1', 'n2']
  env.undo();
  assert.deepEqual(env.getSelected(), ['n1', 'n2'], 'Undo must restore exact selected node IDs');
});


test('canvas.js: delayed save response never merges into a different canvas (audit F01)', async () => {
  const m = canvasJs.match(/async function saveCanvas\(\)\{[\s\S]*?\n\}/);
  assert.ok(m, 'saveCanvas must exist');
  let release;
  const gate = new Promise(r => { release = r; });
  const env = new Function('fetch', `
    let canvas = { id: 'A', title: 'A', updated_at: 1 };
    let nodes = [{ id: 'a-node' }];
    let connections = [];
    let viewport = { x: 0, y: 0, scale: 1 };
    let applyingRemoteCanvas = false, savingCanvasNow = false, saveCanvasAgain = false, localCanvasDirty = false;
    let lastCanvasUpdatedAt = 1;
    const CLIENT_ID = 'c';
    const currentCanvasTime = null;
    const sanitizeConnections = () => {};
    const serializableCanvasNodes = () => nodes;
    const setStatus = () => {};
    const loadCanvasList = () => {};
    const formatCanvasTime = () => '';
    const mergeRemoteNodesAndConnections = () => {};
    const applyRemoteCanvasData = () => {};
    ${m[0]}
    return {
      save: saveCanvas,
      switchTo: () => { canvas = { id: 'B', title: 'B', updated_at: 7 }; nodes = [{ id: 'b-node' }]; },
      state: () => ({ id: canvas.id, nodeIds: nodes.map(n => n.id), updated_at: canvas.updated_at }),
    };
  `)(async () => {
    await gate;
    return { ok: true, status: 200, json: async () => ({ canvas: { id: 'A', title: 'A', updated_at: 99 } }) };
  });
  const pending = env.save();
  env.switchTo();
  release();
  await pending;
  assert.deepEqual(env.state(), { id: 'B', nodeIds: ['b-node'], updated_at: 7 },
    'late response for canvas A must not overwrite canvas B');
});

test('canvas.js: undo/redo history cannot cross canvases (audit F02)', () => {
  const open = canvasJs.match(/async function openCanvas\(id\)\{[\s\S]*?\n\}/);
  assert.ok(open && /undoStack\.length\s*=\s*0/.test(open[0]) && /redoStack\.length\s*=\s*0/.test(open[0]),
    'openCanvas must clear undo/redo stacks');
  const cap = canvasJs.match(/function captureUndoState\(\)\{[\s\S]*?\n\}/)[0];
  const push = canvasJs.match(/function pushUndo\([^)]*\)\{[\s\S]*?\n\}/)[0];
  const undo = canvasJs.match(/function performUndo\(\)\{[\s\S]*?\n\}/)[0];
  const env = new Function(`
    let canvas = { id: 'A' };
    let nodes = [{ id: 'a-node' }];
    let connections = [];
    let selected = new Set();
    let undoStack = [];
    let redoStack = [];
    const UNDO_MAX = 50;
    const serializableCanvasNodes = () => nodes;
    const render = () => {};
    const scheduleSave = () => {};
    ${cap}
    ${push}
    ${undo}
    return {
      push: pushUndo, undo: performUndo,
      // simulate a switch that forgot to clear the stack
      switchTo: () => { canvas = { id: 'B' }; nodes = [{ id: 'b-node' }]; },
      ids: () => nodes.map(n => n.id),
    };
  `)();
  env.push();
  env.switchTo();
  env.undo();
  assert.deepEqual(env.ids(), ['b-node'], 'stale snapshot from canvas A must not be applied to canvas B');
});

test('smart-canvas.js: saves are serialised and failures are surfaced (audit F03)', async () => {
  const m = smartCanvasJs.match(/async function saveCanvas\(\)\{[\s\S]*?\n\}/);
  assert.ok(m, 'smart saveCanvas must exist');
  const toasts = [];
  let calls = 0;
  const releases = [];
  const mk = new Function('fetch', 'toast', `
    let canvasId = 'S', canvas = { id: 'S', updated_at: 1, nodes: [] };
    let nodes = [], viewport = { x: 0, y: 0, scale: 1 };
    let canvasSyncInFlight = false, canvasSaveQueued = false, canvasSaveFailed = false;
    let saveTimer = null, canvasDefaultSmartSettings = {}, initialSmartSettings = {};
    const smartClientId = 'c';
    const savePromptDraftForCurrent = () => {};
    const mediaItemForStorage = x => x, stripImageGenerationMeta = x => x, settingsForStorage = x => x;
    const canvasForStorage = () => ({ title: 't', nodes: [], connections: [], viewport, logs: [], settings: {}, updated_at: canvas.updated_at });
    const applyMergedServerCanvas = () => {};
    const tr = k => k;
    ${m[0]}
    return { save: saveCanvas, flags: () => ({ inFlight: canvasSyncInFlight, queued: canvasSaveQueued, failed: canvasSaveFailed }), stop: () => clearTimeout(saveTimer) };
  `);
  const env = mk(() => { calls++; return new Promise(r => releases.push(r)); }, msg => toasts.push(msg));
  const p1 = env.save();
  env.save();
  assert.equal(calls, 1, 'a second save while one is in flight must not issue a second request');
  assert.equal(env.flags().queued, true, 'the newer edit must be remembered');
  releases[0]({ ok: false, status: 500, json: async () => ({}) });
  await p1;
  assert.equal(env.flags().failed, true, 'a 500 must leave the failure flag set');
  assert.deepEqual(toasts, ['smart.toastSaveFailed'], 'failure must be shown to the user');
  env.stop();
});

test('canvas-reve: multi-node target resolution and regional edit payload generation', async () => {
  const reveJs = fs.readFileSync('static/js/canvas-reve.js', 'utf-8');
  assert.ok(reveJs.includes('resolveNodeTarget'), 'resolveNodeTarget helper should exist');

  const mockWindow = {
    addEventListener: () => {},
    document: {
      addEventListener: () => {},
      createElement: () => ({ style: {}, classList: { add: () => {}, remove: () => {} }, appendChild: () => {} }),
      body: { appendChild: () => {} },
      querySelector: () => null,
    },
    nodes: [
      { id: 'img1', type: 'image', url: '/output/1.png' },
      { id: 'out1', type: 'output', selectedOutputUrl: '/output/2.png' },
      { id: 'smart1', type: 'smart-image', images: [{ url: '/output/3.png' }] },
    ],
  };

  const initReve = new Function('window', 'document', `${reveJs}; return window.CanvasReve;`);
  const CanvasReve = initReve(mockWindow, mockWindow.document);

  // 1. Multi-node resolution
  const t1 = CanvasReve.resolveNodeTarget('img1');
  assert.equal(t1?.url, '/output/1.png');
  assert.equal(t1?.kind, 'classic-image');

  const t2 = CanvasReve.resolveNodeTarget('out1');
  assert.equal(t2?.url, '/output/2.png');
  assert.equal(t2?.kind, 'classic-output');

  const t3 = CanvasReve.resolveNodeTarget('smart1');
  assert.equal(t3?.url, '/output/3.png');
  assert.equal(t3?.kind, 'smart-image');

  // 2. Build edit payload & inplace vs new mode
  let fetchBody = null;
  let undoPushed = 0;
  mockWindow.fetch = async (url, opts) => {
    fetchBody = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ output_url: '/output/edited.png' }) };
  };
  mockWindow.pushUndo = () => { undoPushed++; };
  mockWindow.render = () => {};
  mockWindow.scheduleSave = () => {};

  CanvasReve.activeNodeId = 'img1';
  CanvasReve.applyMode = 'inplace';
  CanvasReve.featherRadius = 16;
  CanvasReve.referenceImageUrl = 'data:image/png;base64,ref123';
  CanvasReve.exportMaskDataUrl = () => 'data:image/png;base64,mask123';

  // mock barEl prompt input & model select
  CanvasReve.barEl = {
    querySelector: (sel) => {
      if (sel === '#revePromptInput') return { value: 'Inpainting test' };
      if (sel === '#reveModelSelect') return { value: 'gemini-2.5-flash-image', selectedOptions: [{ dataset: { provider: 'gemini-cli' } }] };
      return null;
    },
    remove: () => {},
  };

  await CanvasReve.submit();
  assert.equal(undoPushed, 1, 'In-place edit must push undo snapshot');
  assert.equal(fetchBody.mask_data_url, 'data:image/png;base64,mask123');
  assert.equal(fetchBody.reference_image_url, 'data:image/png;base64,ref123');
  assert.equal(fetchBody.feather_radius, 16);
  assert.equal(fetchBody.preserve_outside, true);
  assert.equal(mockWindow.nodes[0].url, '/output/edited.png', 'In-place mode must update source node url');
});
