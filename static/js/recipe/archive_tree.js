(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.ArchiveTree = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';

  function parsePath(value) {
    if (typeof value !== 'string' || !value || value.startsWith('/') || value.includes('\\') || /[\u0000-\u001f\u007f-\u009f]/.test(value)) throw new Error('Unsafe archive path');
    const directory = value.endsWith('/');
    const plain = directory ? value.slice(0, -1) : value;
    const parts = plain.split('/');
    if (!plain || plain.endsWith('/') || parts.some(part => !part || part === '.' || part === '..') || /^[A-Za-z]:\//.test(value)) throw new Error('Unsafe archive path');
    return {path:value, parts, kind:directory ? 'directory' : 'file'};
  }

  function parentPath(path) {
    const parsed = parsePath(path);
    return parsed.parts.length === 1 ? '' : `${parsed.parts.slice(0, -1).join('/')}/`;
  }

  function selectorMatches(selector, candidate) {
    const selected = parsePath(selector);
    const entry = parsePath(candidate);
    if (selected.kind === 'file') return entry.kind === 'file' && selected.parts.join('/') === entry.parts.join('/');
    if (selected.parts.some((part, index) => entry.parts[index] !== part)) return false;
    return entry.parts.length > selected.parts.length || entry.kind === 'directory';
  }

  function canonicalSelectors(values) {
    const parsed = [];
    const seen = new Set();
    const kinds = new Map();
    (values || []).forEach(value => {
      const item = parsePath(String(value));
      const key = item.parts.join('/');
      if (kinds.has(key) && kinds.get(key) !== item.kind) throw new Error('Conflicting archive selector kinds');
      kinds.set(key, item.kind);
      if (!seen.has(item.path)) { parsed.push(item); seen.add(item.path); }
    });
    const directories = parsed.filter(item => item.kind === 'directory');
    return parsed.filter(item => !directories.some(directory => directory.path !== item.path && selectorMatches(directory.path, item.path)))
      .map(item => item.path).sort((left, right) => left < right ? -1 : left > right ? 1 : 0);
  }

  function normalizePayload(payload = {}) {
    const mode = payload.mode === 'entire_archive' ? 'entire_archive' : 'paths';
    const legacy = payload.legacy_file_layout === 'basename';
    const legacyInclude = [];
    if (legacy) {
      const seen = new Set();
      (payload.include || []).forEach(value => {
        const parsed = parsePath(String(value));
        if (parsed.kind !== 'file') throw new Error('Legacy archive payload only supports files');
        if (!seen.has(parsed.path)) { legacyInclude.push(parsed.path); seen.add(parsed.path); }
      });
    }
    const include = mode === 'entire_archive' ? [] : legacy ? legacyInclude : canonicalSelectors(payload.include || []);
    const exclude = canonicalSelectors(payload.exclude || []);
    return {mode, include, exclude, ...(legacy ? {legacy_file_layout:'basename'} : {})};
  }

  function buildTree(inventory = {}) {
    if (inventory.complete !== true || !Array.isArray(inventory.entries)) throw new Error('Archive inventory is incomplete');
    const byPath = new Map();
    inventory.entries.forEach(entry => {
      const parsed = parsePath(entry.path);
      if (parsed.kind !== entry.kind || byPath.has(entry.path)) throw new Error('Invalid archive inventory entry');
      byPath.set(entry.path, {...entry, parent:parentPath(entry.path), depth:parsed.parts.length - 1, children:[]});
    });
    const roots = [];
    byPath.forEach(node => {
      if (!node.parent) roots.push(node);
      else {
        const parent = byPath.get(node.parent);
        if (!parent || parent.kind !== 'directory') throw new Error(`Missing archive directory: ${node.parent}`);
        parent.children.push(node);
      }
    });
    const sortNodes = nodes => nodes.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0).forEach(node => sortNodes(node.children));
    sortNodes(roots);
    return {inventory, byPath, roots};
  }

  function visibleNodes(tree, expanded = new Set()) {
    const rows = [];
    const visit = node => {
      rows.push(node);
      if (node.kind === 'directory' && expanded.has(node.path)) node.children.forEach(visit);
    };
    tree.roots.forEach(visit);
    return rows;
  }

  function createState(payload = {}) {
    return {payload:normalizePayload(payload), tree:null, inventory:null, expanded:new Set(), stale:true, selectionError:null};
  }

  function setInventory(state, inventory, selectionError = null) {
    state.inventory = inventory;
    state.tree = buildTree(inventory);
    state.expanded = new Set();
    state.stale = false;
    state.selectionError = selectionError;
    return state;
  }

  function markStale(state) {
    state.stale = true;
    state.expanded.clear();
    return state;
  }

  function mutate(state) {
    if (state.payload.legacy_file_layout) state.payload.include = canonicalSelectors(state.payload.include);
    delete state.payload.legacy_file_layout;
    state.selectionError = null;
  }

  function setMode(state, mode) {
    const next = mode === 'entire_archive' ? 'entire_archive' : 'paths';
    if (next === state.payload.mode) return false;
    const previous = state.payload.mode;
    state.payload.mode = next;
    state.payload.include = [];
    if (previous === 'entire_archive' && next === 'paths') state.payload.exclude = [];
    mutate(state);
    return true;
  }

  function indexedEntry(state, path) {
    if (state.stale || !state.tree) return null;
    return state.tree.byPath.get(path) || null;
  }

  function includePath(state, path) {
    if (state.payload.mode !== 'paths' || !indexedEntry(state, path)) return false;
    const next = canonicalSelectors([...state.payload.include, path]);
    if (JSON.stringify(next) === JSON.stringify(state.payload.include)) return false;
    state.payload.include = next;
    state.payload.exclude = state.payload.exclude.filter(excluded => next.some(included => selectorMatches(included, excluded)));
    mutate(state);
    return true;
  }

  function removeInclude(state, path) {
    if (!state.payload.include.includes(path)) return false;
    state.payload.include = state.payload.include.filter(item => item !== path);
    state.payload.exclude = state.payload.exclude.filter(excluded => state.payload.include.some(included => selectorMatches(included, excluded)));
    mutate(state);
    return true;
  }

  function canExclude(state, path) {
    if (!indexedEntry(state, path)) return false;
    if (state.payload.exclude.some(selector => selectorMatches(selector, path))) return false;
    if (state.payload.mode === 'entire_archive') return true;
    return state.payload.include.some(selector => parsePath(selector).kind === 'directory' && selectorMatches(selector, path) && selector !== path);
  }

  function excludePath(state, path) {
    if (!canExclude(state, path)) return false;
    state.payload.exclude = canonicalSelectors([...state.payload.exclude, path]);
    mutate(state);
    return true;
  }

  function removeExclude(state, path) {
    if (!state.payload.exclude.includes(path)) return false;
    state.payload.exclude = state.payload.exclude.filter(item => item !== path);
    mutate(state);
    return true;
  }

  function toggleExpanded(state, path) {
    const entry = indexedEntry(state, path);
    if (!entry || entry.kind !== 'directory') return false;
    if (state.expanded.has(path)) state.expanded.delete(path); else state.expanded.add(path);
    return true;
  }

  function selectedFile(state, path) {
    const included = state.payload.mode === 'entire_archive' || state.payload.include.some(selector => selectorMatches(selector, path));
    return included && !state.payload.exclude.some(selector => selectorMatches(selector, path));
  }

  function selectionSummary(state) {
    const include = state.payload.include.map(parsePath);
    const exclude = state.payload.exclude.map(parsePath);
    const resolvedFiles = !state.stale && state.inventory
      ? state.inventory.entries.filter(entry => entry.kind === 'file' && selectedFile(state, entry.path)).length
      : null;
    const missing = !state.stale && state.tree
      ? [...state.payload.include, ...state.payload.exclude].filter(path => !state.tree.byPath.has(path))
      : [];
    return {
      mode:state.payload.mode,
      selectedDirectories:include.filter(item => item.kind === 'directory').length,
      explicitFiles:include.filter(item => item.kind === 'file').length,
      excludedDirectories:exclude.filter(item => item.kind === 'directory').length,
      excludedFiles:exclude.filter(item => item.kind === 'file').length,
      exclusionCount:exclude.length, resolvedFiles, missing,
    };
  }

  function entryState(state, path) {
    const parsed = parsePath(path);
    const exactExclude = state.payload.exclude.includes(path);
    const excluded = state.payload.exclude.some(selector => selectorMatches(selector, path));
    const exactInclude = state.payload.include.includes(path);
    const insideInclude = state.payload.mode === 'entire_archive' || state.payload.include.some(selector => selectorMatches(selector, path));
    const contains = [...state.payload.include, ...state.payload.exclude].some(selector => selector !== path && selectorMatches(path, selector));
    if (exactExclude) return parsed.kind === 'directory' ? 'Excluded recursively' : 'Excluded';
    if (excluded) return 'Excluded';
    if (exactInclude) return parsed.kind === 'directory' ? 'Included recursively' : 'Included';
    if (contains) return 'Contains selections';
    if (insideInclude) return '';
    return 'Available';
  }

  return {
    parsePath, parentPath, selectorMatches, canonicalSelectors, normalizePayload,
    buildTree, visibleNodes, createState, setInventory, markStale, setMode,
    includePath, removeInclude, canExclude, excludePath, removeExclude,
    toggleExpanded, selectionSummary, entryState,
  };
});
