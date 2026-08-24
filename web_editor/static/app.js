// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
//
// UI layer: talks to the Python server over JSON, renders the tree, inspector,
// USDA pane and Python console, and drives the three.js viewport.

import { Viewport } from './viewport.js';

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const kid of kids.flat()) {
    if (kid != null) node.append(kid.nodeType ? kid : String(kid));
  }
  return node;
};

const PRIM_TYPES = ['Xform', 'Scope', 'Mesh', 'Sphere', 'Cube', 'Cylinder', 'Cone',
  'Capsule', 'Plane', 'Camera', 'DistantLight', 'SphereLight', 'RectLight',
  'PointInstancer', 'Material', 'Shader', ''];

const state = {
  stage: null,
  scenegraph: null,
  selection: null,
  detail: null,
  expanded: new Set(['/']),
  inspectTab: 'properties',
  dockTab: 'usda',
  usdaMode: 'root',
  history: [],
  historyIndex: 0,
};

let viewport;

// ---------------------------------------------------------------- transport

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });

function toast(message, isError = false) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.toggle('error', isError);
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, isError ? 6000 : 2600);
}

/** Run an action, refresh from its response, and surface failures as toasts. */
async function act(fn, { refreshViewport = true } = {}) {
  try {
    const data = await fn();
    if (data.stage) applyStage(data.stage);
    if (data.scenegraph) {
      state.scenegraph = data.scenegraph;
      renderTree();
    }
    if ('selection' in data) await select(data.selection);
    if (refreshViewport) await refreshGeometry();
    await refreshDock();
    if (state.selection) await refreshDetail();
    return data;
  } catch (err) {
    toast(err.message, true);
    throw err;
  }
}

// -------------------------------------------------------------- stage state

function applyStage(stage) {
  state.stage = stage;
  $('#stage-path').textContent = stage.path || 'untitled (in memory)';
  $('#dirty-dot').hidden = !stage.dirty;
  $('#btn-undo').disabled = !stage.canUndo;
  $('#btn-redo').disabled = !stage.canRedo;
  viewport?.setUpAxis(stage.upAxis);

  const select = $('#edit-target');
  select.replaceChildren(...stage.layers.map((layer) =>
    el('option', { value: layer.identifier, textContent: layer.display + (layer.dirty ? ' *' : '') })));
  // Always mirror the stage's real edit target rather than the previous
  // selection -- opening a file replaces the layer stack entirely.
  select.value = stage.editTarget;
}

/** Open the first few levels so a freshly loaded stage isn't a single row. */
function autoExpand(node, depth = 0) {
  if (depth > 2 || !node.children.length) return;
  state.expanded.add(node.path);
  for (const child of node.children) autoExpand(child, depth + 1);
}

async function refreshAll() {
  const data = await api('/api/stage');
  applyStage(data.stage);
  state.scenegraph = data.scenegraph;
  autoExpand(state.scenegraph);
  renderTree();
  await refreshDetail();   // paints the inspector's empty state on first load
  await refreshGeometry();
  await refreshDock();
}

async function refreshGeometry() {
  try {
    const { items } = await api('/api/geometry');
    const count = viewport.build(items);
    $('#view-stats').textContent = `${items.length} prim${items.length === 1 ? '' : 's'} drawn`
      + (count !== items.length ? ` (${count} objects)` : '');
  } catch (err) {
    toast(`Viewport: ${err.message}`, true);
  }
}

// --------------------------------------------------------------------- tree

function renderTree() {
  const container = $('#tree');
  container.replaceChildren();
  if (!state.scenegraph) return;
  for (const child of state.scenegraph.children) {
    container.append(...treeRows(child, 0));
  }
  if (!state.scenegraph.children.length) {
    container.append(el('div', { className: 'empty' }, 'Empty stage. Use ＋ to add a prim.'));
  }
}

function treeRows(node, depth) {
  const rows = [];
  const hasKids = node.children.length > 0;
  const open = state.expanded.has(node.path);

  const twisty = el('span', {
    className: 'twisty' + (hasKids ? '' : ' leaf'),
    textContent: open ? '▾' : '▸',
  });
  twisty.onclick = (e) => {
    e.stopPropagation();
    state.expanded.has(node.path)
      ? state.expanded.delete(node.path)
      : state.expanded.add(node.path);
    renderTree();
  };

  const row = el('div', {
    className: [
      'node-row',
      node.path === state.selection ? 'selected' : '',
      node.active ? '' : 'inactive',
      node.visible ? '' : 'invisible',
      node.isInstanceProxy ? 'proxy' : '',
    ].filter(Boolean).join(' '),
    title: node.path + (node.isInstanceProxy ? '  (instance proxy — read-only)' : ''),
  },
    twisty,
    el('span', { className: 'node-name', textContent: node.name }),
    el('span', { className: 'node-type', textContent: node.type }),
    ...node.arcs.map((arc) => el('span', { className: `arc-chip ${arc}`, textContent: arc })),
  );
  row.style.paddingLeft = `${4 + depth * 14}px`;
  row.onclick = () => select(node.path);
  row.ondblclick = () => viewport.frame(node.path);
  rows.push(row);

  if (open) for (const kid of node.children) rows.push(...treeRows(kid, depth + 1));
  return rows;
}

async function select(path) {
  state.selection = path;
  viewport.applySelection(path);
  renderTree();
  // Mirror the selection into the URL so a prim can be linked or bookmarked.
  const hash = path ? `#${path}` : '';
  if (location.hash !== hash) history.replaceState(null, '', location.pathname + hash);
  await refreshDetail();
}

async function refreshDetail() {
  const body = $('#inspect-body');
  if (!state.selection) {
    state.detail = null;
    $('#inspect-title').textContent = 'Inspector';
    body.replaceChildren(el('div', { className: 'empty' }, 'Select a prim.'));
    return;
  }
  try {
    state.detail = await api(`/api/prim?path=${encodeURIComponent(state.selection)}`);
  } catch (err) {
    body.replaceChildren(el('div', { className: 'empty' }, err.message));
    return;
  }
  $('#inspect-title').textContent = state.detail.name || state.selection;
  renderInspector();
}

// ---------------------------------------------------------------- inspector

function renderInspector() {
  const body = $('#inspect-body');
  body.replaceChildren();
  if (!state.detail) return;
  if (state.inspectTab === 'properties') renderProperties(body);
  else renderComposition(body);
}

function group(title, ...children) {
  return el('div', { className: 'group' }, el('h4', { textContent: title }), ...children);
}

function field(label, control) {
  return el('div', { className: 'field' }, el('label', { textContent: label }), control);
}

function readonlyField(label, value) {
  return field(label, el('div', { className: 'ro', textContent: String(value ?? '—') }));
}

function vectorInput(values, onCommit) {
  const inputs = values.map((v) => el('input', {
    type: 'number', step: '0.1', value: Number(v).toFixed(4).replace(/\.?0+$/, ''),
  }));
  for (const input of inputs) {
    input.onchange = () => onCommit(inputs.map((i) => parseFloat(i.value) || 0));
    input.onkeydown = (e) => { if (e.key === 'Enter') input.blur(); };
  }
  return el('div', { className: 'vec' }, ...inputs);
}

function renderProperties(body) {
  const d = state.detail;
  const locked = d.isInstanceProxy;

  // --- identity
  const typeSelect = el('select');
  typeSelect.replaceChildren(...PRIM_TYPES.map((t) =>
    el('option', { value: t, textContent: t || '(typeless)' })));
  if (!PRIM_TYPES.includes(d.type)) {
    typeSelect.append(el('option', { value: d.type, textContent: d.type }));
  }
  typeSelect.value = d.type;
  typeSelect.disabled = locked;
  typeSelect.onchange = () => act(() => post('/api/prim',
    { action: 'metadata', path: d.path, key: 'typeName', value: typeSelect.value }));

  const nameInput = el('input', { type: 'text', value: d.name, disabled: locked });
  nameInput.onchange = () => act(() => post('/api/prim',
    { action: 'rename', path: d.path, name: nameInput.value }));

  const kindInput = el('input', { type: 'text', value: d.kind, placeholder: '(unset)', disabled: locked });
  kindInput.onchange = () => act(() => post('/api/prim',
    { action: 'metadata', path: d.path, key: 'kind', value: kindInput.value }));

  const activeBox = el('input', { type: 'checkbox', checked: d.active, disabled: locked });
  activeBox.onchange = () => act(() => post('/api/prim',
    { action: 'metadata', path: d.path, key: 'active', value: activeBox.checked }));

  const instBox = el('input', { type: 'checkbox', checked: d.instanceable, disabled: locked });
  instBox.onchange = () => act(() => post('/api/prim',
    { action: 'metadata', path: d.path, key: 'instanceable', value: instBox.checked }));

  body.append(group('Prim',
    field('Name', nameInput),
    field('Type', typeSelect),
    readonlyField('Path', d.path),
    readonlyField('Specifier', d.specifier),
    field('Kind', kindInput),
    field('Active', activeBox),
    field('Instanceable', instBox),
    d.appliedSchemas.length ? readonlyField('Applied APIs', d.appliedSchemas.join(', ')) : null,
    locked ? el('div', { className: 'muted', textContent:
      'Instance proxy — edit the prototype source to change this.' }) : null,
  ));

  // --- transform
  if (d.transform) {
    const t = d.transform;
    const send = (key, values) => act(() => post('/api/prim',
      { action: 'transform', path: d.path, [key]: values }));
    body.append(group('Transform (XformCommonAPI)',
      field('Translate', vectorInput(t.translate, (v) => send('translate', v))),
      field('Rotate', vectorInput(t.rotate, (v) => send('rotate', v))),
      field('Scale', vectorInput(t.scale, (v) => send('scale', v))),
      readonlyField('Rotation order', t.rotationOrder),
    ));
  }

  // --- attributes
  const authored = d.attributes.filter((a) => a.authored);
  const rest = d.attributes.filter((a) => !a.authored);
  const rows = [...authored, ...rest].map((attr) => attributeRow(d, attr, locked));
  body.append(group(`Attributes (${authored.length} authored / ${d.attributes.length})`,
    rows.length ? rows : el('div', { className: 'muted', textContent: 'None.' })));

  if (d.relationships.length) {
    body.append(group('Relationships', ...d.relationships.map((rel) =>
      readonlyField(rel.name, rel.targets.join(', ') || '(no targets)'))));
  }
}

function attributeRow(detail, attr, locked) {
  const label = el('span', { className: 'name' },
    el('span', { textContent: attr.name, title: `${attr.name} : ${attr.typeName}` }),
    attr.authored ? el('span', { className: 'badge authored', textContent: 'A' }) : null,
    attr.timeSamples ? el('span', { className: 'badge', textContent: `${attr.timeSamples}t` }) : null,
  );
  label.style.cursor = 'pointer';
  label.title = 'Click to show the per-layer opinion stack';
  label.onclick = () => showAttributeStack(detail.path, attr.name);

  let control;
  const commit = (value) => act(() => post('/api/prim',
    { action: 'attribute', path: detail.path, name: attr.name, value }));

  if (locked || !attr.editable) {
    control = el('div', { className: 'ro', textContent: formatValue(attr.value) });
  } else if (attr.typeName === 'bool') {
    control = el('input', { type: 'checkbox', checked: !!attr.value });
    control.onchange = () => commit(control.checked);
  } else if (Array.isArray(attr.value)) {
    control = vectorInput(attr.value, commit);
  } else if (['float', 'double', 'half', 'int', 'int64', 'uint'].includes(attr.typeName)) {
    control = el('input', { type: 'number', step: '0.1', value: attr.value ?? 0 });
    control.onchange = () => commit(parseFloat(control.value) || 0);
  } else {
    control = el('input', { type: 'text', value: attr.value ?? '' });
    control.onchange = () => commit(control.value);
  }

  const row = el('div', { className: 'field attr-row' }, label, control);
  return row;
}

function formatValue(value) {
  if (value == null) return '—';
  if (Array.isArray(value)) {
    const flat = JSON.stringify(value);
    return flat.length > 60 ? `${flat.slice(0, 57)}…` : flat;
  }
  return String(value);
}

async function showAttributeStack(primPath, attrName) {
  try {
    const data = await api(`/api/composition?path=${encodeURIComponent(primPath)}`
      + `&attribute=${encodeURIComponent(attrName)}`);
    const body = data.opinions.length
      ? el('div', {}, ...data.opinions.map((op, i) =>
        el('div', { className: 'stack-entry' + (i === 0 ? ' winner' : '') },
          el('div', { className: 'layer', textContent: op.layer + (i === 0 ? '  ← winning' : '') }),
          el('div', { className: 'meta', textContent: `${op.path} = ${formatValue(op.value)}` }))))
      : el('div', { className: 'muted', textContent: 'No authored opinions — this is the schema fallback.' });
    openModal(`Opinions for ${attrName}`, body, null);
  } catch (err) {
    toast(err.message, true);
  }
}

// -------------------------------------------------------------- composition

function renderComposition(body) {
  const d = state.detail;
  const c = d.composition;

  body.append(group('Prim stack (strongest first)',
    ...(c.primStack.length
      ? c.primStack.map((spec, i) => el('div', { className: 'stack-entry' + (i === 0 ? ' winner' : '') },
        el('div', { className: 'layer', textContent: spec.layer }),
        el('div', { className: 'meta', textContent: `${spec.specifier} ${spec.path}` })))
      : [el('div', { className: 'muted', textContent: 'No specs.' })])));

  // --- variant sets
  const variantNodes = c.variantSets.map((vset) => {
    const picker = el('select');
    picker.replaceChildren(
      el('option', { value: '', textContent: '(none)' }),
      ...vset.variants.map((v) => el('option', { value: v, textContent: v })));
    picker.value = vset.selection || '';
    picker.onchange = () => act(() => post('/api/composition',
      { action: 'setVariant', path: d.path, name: vset.name, variant: picker.value }));
    return field(vset.name, picker);
  });
  const addVariantSet = el('button', { textContent: '+ Variant set' });
  addVariantSet.onclick = () => promptVariantSet(d.path);
  body.append(group('Variant sets',
    ...(variantNodes.length ? variantNodes : [el('div', { className: 'muted', textContent: 'None.' })]),
    addVariantSet));

  // --- arcs
  const arcList = [
    ...c.references.map((r) => ({ ...r, arc: 'reference' })),
    ...c.payloads.map((p) => ({ ...p, arc: 'payload' })),
  ];
  const addArc = el('button', { textContent: '+ Reference / payload' });
  addArc.onclick = () => promptArc(d.path);
  body.append(group('References & payloads',
    ...(arcList.length
      ? arcList.map((a) => el('div', { className: 'stack-entry' },
        el('div', { className: 'layer', textContent: `${a.arc}: ${a.assetPath || '(internal)'}` }),
        el('div', { className: 'meta', textContent: `${a.primPath || 'defaultPrim'} · in ${a.layer}` })))
      : [el('div', { className: 'muted', textContent: 'None.' })]),
    addArc));

  // --- layer stack
  const addSublayer = el('button', { textContent: '+ Sublayer' });
  addSublayer.onclick = () => promptSublayer();
  const setDefault = el('button', { textContent: 'Set as default prim' });
  setDefault.onclick = () => act(() => post('/api/stage',
    { action: 'defaultPrim', path: d.path }));
  body.append(group('Stage',
    readonlyField('Default prim', state.stage?.defaultPrim),
    readonlyField('Up axis', state.stage?.upAxis),
    readonlyField('Meters per unit', state.stage?.metersPerUnit),
    el('div', { className: 'vec' }, addSublayer, setDefault)));
}

// ------------------------------------------------------------------- modals

function openModal(title, bodyNode, onOk) {
  $('#modal-title').textContent = title;
  $('#modal-body').replaceChildren(bodyNode);
  $('#modal-backdrop').hidden = false;
  const okButton = $('[data-modal=ok]');
  okButton.hidden = !onOk;
  openModal._ok = onOk;
  (bodyNode.querySelector('input,select') || okButton)?.focus();
}

function closeModal() {
  $('#modal-backdrop').hidden = true;
  openModal._ok = null;
}

$('[data-modal=cancel]').onclick = closeModal;
$('[data-modal=ok]').onclick = async () => {
  const fn = openModal._ok;
  closeModal();
  if (fn) await fn();
};
$('#modal-backdrop').onclick = (e) => { if (e.target.id === 'modal-backdrop') closeModal(); };

function promptAddPrim() {
  const parent = state.selection || '/';
  const name = el('input', { type: 'text', value: 'Prim' });
  const type = el('select');
  type.replaceChildren(...PRIM_TYPES.map((t) =>
    el('option', { value: t, textContent: t || '(typeless / over)' })));
  type.value = 'Xform';
  openModal('Add prim',
    el('div', {},
      field('Parent', el('div', { className: 'ro', textContent: parent })),
      field('Name', name), field('Type', type)),
    () => act(() => post('/api/prim',
      { action: 'create', parent, name: name.value, type: type.value })));
}

function promptArc(primPath) {
  const asset = el('input', { type: 'text', placeholder: './asset.usda' });
  const target = el('input', { type: 'text', placeholder: '(defaultPrim)' });
  const arc = el('select');
  arc.replaceChildren(...['reference', 'payload', 'inherit', 'specialize'].map((a) =>
    el('option', { value: a, textContent: a })));
  const browse = el('button', { textContent: 'Browse…' });
  browse.onclick = async () => {
    const picked = await browseForFile();
    if (picked) asset.value = picked;
  };
  openModal('Add composition arc',
    el('div', {},
      field('Arc', arc),
      field('Asset path', el('div', { className: 'vec' }, asset, browse)),
      field('Target prim', target),
      el('div', { className: 'muted', textContent:
        'Inherits and specializes use the target prim path and ignore the asset path.' })),
    () => act(() => post('/api/composition', {
      action: 'arc', path: primPath, arc: arc.value,
      assetPath: asset.value, primPath: target.value,
    })));
}

function promptSublayer() {
  const asset = el('input', { type: 'text', placeholder: './layer.usda' });
  const browse = el('button', { textContent: 'Browse…' });
  browse.onclick = async () => {
    const picked = await browseForFile();
    if (picked) asset.value = picked;
  };
  openModal('Add sublayer',
    el('div', {},
      field('Layer', el('div', { className: 'vec' }, asset, browse)),
      el('div', { className: 'muted', textContent: 'Inserted as the strongest sublayer of the root layer.' })),
    () => act(() => post('/api/composition', { action: 'sublayer', assetPath: asset.value })));
}

function promptVariantSet(primPath) {
  const name = el('input', { type: 'text', value: 'variantSet' });
  const variants = el('input', { type: 'text', value: 'a, b' });
  openModal('Add variant set',
    el('div', {},
      field('Set name', name),
      field('Variants', variants),
      el('div', { className: 'muted', textContent: 'Comma-separated. The first becomes the selection.' })),
    () => act(() => post('/api/composition', {
      action: 'addVariantSet', path: primPath, name: name.value,
      variants: variants.value.split(',').map((v) => v.trim()).filter(Boolean),
    })));
}

/** Server-side file browser; resolves to a path string or null. */
function browseForFile(startPath = '') {
  return new Promise((resolve) => {
    const list = el('div', { className: 'browse-list' });
    const current = el('div', { className: 'muted' });
    const manual = el('input', { type: 'text', placeholder: 'or type a path…' });

    const load = async (path) => {
      try {
        const data = await api(`/api/browse?path=${encodeURIComponent(path || '')}`);
        current.textContent = data.cwd;
        manual.value = data.cwd;
        const rows = [];
        if (data.parent) rows.push(row({ name: '..', path: data.parent, dir: true }));
        rows.push(...data.entries.map(row));
        list.replaceChildren(...rows);
      } catch (err) { toast(err.message, true); }
    };
    const row = (entry) => {
      const node = el('div', { className: 'browse-row' },
        el('span', { className: 'icon', textContent: entry.dir ? '▸' : '·' }),
        el('span', { textContent: entry.name }));
      node.onclick = () => {
        if (entry.dir) load(entry.path);
        else { closeModal(); resolve(entry.path); }
      };
      return node;
    };

    openModal('Choose a USD file',
      el('div', {}, current, list, el('div', { style: 'margin-top:8px' }, manual)),
      () => resolve(manual.value || null));
    $('[data-modal=cancel]').onclick = () => { closeModal(); resolve(null); };
    load(startPath);
  });
}

// --------------------------------------------------------------------- dock

async function refreshDock() {
  if (state.dockTab === 'usda') await renderUsda();
}

async function renderUsda() {
  const tools = $('#dock-tools');
  const picker = el('select');
  picker.replaceChildren(
    el('option', { value: 'root', textContent: 'Root layer' }),
    el('option', { value: 'flattened', textContent: 'Flattened (composed)' }),
    ...(state.stage?.layers || []).map((l) =>
      el('option', { value: `layer:${l.identifier}`, textContent: l.display })));
  picker.value = state.usdaMode;
  picker.onchange = () => { state.usdaMode = picker.value; renderUsda(); };
  tools.replaceChildren(picker);

  const [mode, identifier] = state.usdaMode.startsWith('layer:')
    ? ['layer', state.usdaMode.slice(6)] : [state.usdaMode, ''];
  try {
    const data = await api(`/api/usda?mode=${mode}&identifier=${encodeURIComponent(identifier)}`);
    $('#dock-body').replaceChildren(el('pre', { className: 'code', textContent: data.text }));
  } catch (err) {
    $('#dock-body').replaceChildren(el('pre', { className: 'code', textContent: err.message }));
  }
}

function renderConsole() {
  $('#dock-tools').replaceChildren(
    el('span', { className: 'muted', textContent: 'stage, Usd, UsdGeom, Sdf, Gf in scope · Enter runs · Shift+Enter newline' }));

  const out = el('pre', { id: 'console-out' });
  const input = el('textarea', {
    id: 'console-in', className: 'code',
    placeholder: ">>> stage.GetPrimAtPath('/World')",
    spellcheck: false,
  });

  const write = (text, cls) => {
    out.append(el('span', { className: cls, textContent: text.endsWith('\n') ? text : text + '\n' }));
    out.scrollTop = out.scrollHeight;
  };

  input.onkeydown = async (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const code = input.value.trim();
      if (!code) return;
      state.history.push(code);
      state.historyIndex = state.history.length;
      input.value = '';
      write(`>>> ${code}`, 'in');
      try {
        const data = await post('/api/python', { code });
        if (data.stdout) write(data.stdout.replace(/\n$/, ''), '');
        if (data.result) write(data.result, 'res');
        if (data.error) write(data.error.trimEnd(), 'err');
        if (data.stage) applyStage(data.stage);
        // A console command can author anything, so resync everything.
        const stageData = await api('/api/stage');
        state.scenegraph = stageData.scenegraph;
        renderTree();
        await refreshGeometry();
        if (state.selection) await refreshDetail();
      } catch (err) {
        write(err.message, 'err');
      }
    } else if (e.key === 'ArrowUp' && !input.value.includes('\n')) {
      if (state.historyIndex > 0) {
        e.preventDefault();
        input.value = state.history[--state.historyIndex] ?? '';
      }
    } else if (e.key === 'ArrowDown' && !input.value.includes('\n')) {
      if (state.historyIndex < state.history.length - 1) {
        e.preventDefault();
        input.value = state.history[++state.historyIndex] ?? '';
      }
    }
  };

  $('#dock-body').replaceChildren(el('div', { id: 'console' }, out, input));
  write('# Python console — runs against the live stage. Edits are undoable.', '');
  input.focus();
}

// ----------------------------------------------------------------- toolbar

const actions = {
  new: () => act(() => post('/api/stage', { action: 'new' })),
  undo: () => act(() => post('/api/stage', { action: 'undo' })),
  redo: () => act(() => post('/api/stage', { action: 'redo' })),
  frame: () => viewport.frame(state.selection),
  'add-prim': promptAddPrim,

  async open() {
    const path = await browseForFile();
    if (path) await act(() => post('/api/stage', { action: 'open', path }));
  },

  async save() {
    if (!state.stage?.path) return actions.saveas();
    const data = await act(() => post('/api/stage', { action: 'save' }));
    if (data) toast(`Saved ${data.stage.path}`);
  },

  async saveas() {
    const input = el('input', { type: 'text', value: state.stage?.path || 'scene.usda' });
    openModal('Save as',
      el('div', {},
        field('Path', input),
        el('div', { className: 'muted', textContent: 'Extension picks the format: .usda text, .usdc binary.' })),
      async () => {
        const data = await act(() => post('/api/stage', { action: 'save', path: input.value }));
        if (data) toast(`Saved ${data.stage.path}`);
      });
  },

  'delete-prim': async () => {
    if (!state.selection) return toast('Nothing selected.', true);
    await act(() => post('/api/prim', { action: 'delete', path: state.selection }));
  },
};

document.addEventListener('click', (e) => {
  const button = e.target.closest('[data-act]');
  if (button) actions[button.dataset.act]?.();
});

$('#edit-target').onchange = (e) => act(() => post('/api/stage',
  { action: 'editTarget', identifier: e.target.value }), { refreshViewport: false });

$('#opt-grid').onchange = (e) => viewport.setGridVisible(e.target.checked);
$('#opt-wire').onchange = (e) => viewport.setWireframe(e.target.checked);

for (const [id, key] of [['#inspect-tabs', 'inspectTab'], ['#dock-tabs', 'dockTab']]) {
  $(id).addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (!tab) return;
    [...$(id).querySelectorAll('.tab')].forEach((t) => t.classList.toggle('active', t === tab));
    state[key] = tab.dataset.tab;
    if (key === 'inspectTab') renderInspector();
    else state.dockTab === 'python' ? renderConsole() : renderUsda();
  });
}

document.addEventListener('keydown', (e) => {
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName);
  if (e.key === 'Escape') return closeModal();
  if (typing) return;
  if (e.key === 'f' || e.key === 'F') viewport.frame(state.selection);
  if (e.key === 'Delete' || e.key === 'Backspace') actions['delete-prim']();
  if ((e.ctrlKey || e.metaKey) && e.key === 'z') { e.preventDefault(); actions[e.shiftKey ? 'redo' : 'undo'](); }
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); actions.save(); }
});

// ----------------------------------------------------------- panel resizing

for (const gutter of document.querySelectorAll('.gutter')) {
  gutter.addEventListener('pointerdown', (down) => {
    down.preventDefault();
    gutter.setPointerCapture(down.pointerId);
    const which = gutter.dataset.resize;
    const move = (e) => {
      const root = document.documentElement.style;
      if (which === 'tree') root.setProperty('--panel-tree', `${Math.max(160, e.clientX)}px`);
      else if (which === 'inspector') root.setProperty('--panel-inspect', `${Math.max(220, innerWidth - e.clientX)}px`);
      else root.setProperty('--dock-h', `${Math.max(90, innerHeight - e.clientY)}px`);
      viewport.resize();
    };
    const up = () => {
      gutter.removeEventListener('pointermove', move);
      gutter.removeEventListener('pointerup', up);
    };
    gutter.addEventListener('pointermove', move);
    gutter.addEventListener('pointerup', up);
  });
}

// ------------------------------------------------------------------- start

/**
 * Stand-in used when WebGL is unavailable (VMs, remote desktops, blocklisted
 * GPUs, headless browsers). Everything except the 3D view keeps working, which
 * matters because the tree, inspector, USDA pane and Python console are useful
 * on their own.
 */
const nullViewport = {
  build: () => 0, clear() {}, resize() {}, frame() {},
  applySelection() {}, setUpAxis() {}, setGridVisible() {}, setWireframe() {},
};

function initViewport() {
  const host = $('#viewport');
  try {
    return new Viewport(host, (path) => select(path));
  } catch (err) {
    host.append(el('div', { className: 'empty', style: 'padding-top:60px' },
      el('div', { textContent: '3D viewport unavailable' }),
      el('div', { className: 'muted', style: 'margin-top:6px', textContent: err.message }),
      el('div', { className: 'muted', style: 'margin-top:6px',
        textContent: 'Everything else — scenegraph, inspector, USDA and the Python console — still works.' })));
    toast('WebGL unavailable; running without the 3D viewport.', true);
    return nullViewport;
  }
}

viewport = initViewport();
refreshAll()
  .then(async () => {
    const linked = decodeURIComponent(location.hash.slice(1));
    if (linked.startsWith('/')) {
      // Expand ancestors so the linked prim is actually visible in the tree.
      const parts = linked.split('/').filter(Boolean);
      for (let i = 0; i < parts.length; i++) {
        state.expanded.add('/' + parts.slice(0, i).join('/'));
      }
      await select(linked);
      viewport.frame(linked);
    } else {
      viewport.frame(null);
    }
  })
  .catch((err) => toast(`Startup failed: ${err.message}`, true));
