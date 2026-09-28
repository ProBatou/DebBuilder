// The canonical GET body is the baseline. No defaults or normalization belong here.
import {ownership} from './fields.js';
export const clone = value => structuredClone(value);

export function equal(a, b) {
  if (Object.is(a, b)) return true;
  if (typeof a !== typeof b || a === null || b === null) return false;
  if (Array.isArray(a) || Array.isArray(b))
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => equal(v, b[i]));
  if (typeof a !== 'object') return false;
  const ak = Object.keys(a), bk = Object.keys(b);
  return ak.length === bk.length && ak.every(key => Object.hasOwn(b, key) && equal(a[key], b[key]));
}

export const segments = path => path.replace(/^\$\.?/, '').replace(/\[(\d+)\]/g, '.$1').split('.').filter(Boolean);
export function readPath(document, path) {return segments(path).reduce((value, part) => value?.[part], document);}
export function allowedPath(path, editablePaths) {
  return editablePaths.some(allowed => path === allowed || path.startsWith(`${allowed}.`) || path.startsWith(`${allowed}[`));
}

export function hydrate(recipe, {managed = false, editablePaths = []} = {}) {
  if (recipe?.schema_version !== 5) throw new TypeError('Expected canonical Recipe v5');
  return {baseline: clone(recipe), draft: clone(recipe), managed, editablePaths: [...editablePaths], modeCache: {}};
}
export const candidate = editor => clone(editor.draft);
export const dirty = editor => !equal(editor.baseline, editor.draft);
export const cancel = editor => ({...editor, draft: clone(editor.baseline), modeCache: {}});

export function change(editor, path, value) {
  if (!ownership(path,editor.managed,editor.editablePaths).startsWith('EDITABLE_')) throw new Error(`Recipe field is read-only: ${path}`);
  const parts = segments(path);
  if (!parts.length || parts[0] === 'management' || parts[0] === 'schema_version') throw new Error(`Recipe field is read-only: ${path}`);
  const draft = clone(editor.draft);
  const modeCache = clone(editor.modeCache || {});
  if (path === 'artifact.mode') {
    const before = draft.artifact.mode;
    modeCache[`artifact.${before}`] = {type:draft.artifact.type, payload:clone(draft.artifact.payload)};
    const saved = modeCache[`artifact.${value}`];
    draft.artifact.type = saved?.type ?? (value === 'upstream_archive' ? 'archive' : 'deb');
    if (value === 'upstream_archive') draft.artifact.payload = clone(saved?.payload ?? {mode:'paths',include:[],exclude:[]});
    else delete draft.artifact.payload;
  }
  if (path === 'build.output.mode') {
    const output = draft.build.output;
    modeCache[`output.${output.mode}`] = {path:output.path, paths:clone(output.paths)};
    const saved = modeCache[`output.${value}`];
    if (value === 'path') output.path = saved?.path ?? '';
    else delete output.path;
    if (value === 'paths') output.paths = clone(saved?.paths ?? []);
    else delete output.paths;
  }
  if (path === 'install.content.source') {
    if (value === 'configured_files') {
      modeCache.installDestination = draft.install.destination;
      draft.install.destination = '';
    } else if (draft.install.content.source === 'configured_files') {
      draft.install.destination = modeCache.installDestination ?? editor.baseline.install.destination;
    }
  }
  if (path === 'artifact.payload.mode' && value === 'entire_archive') {
    modeCache.payloadInclude = clone(draft.artifact.payload.include);
    draft.artifact.payload.include = [];
  }
  if (path === 'artifact.payload.mode' && value === 'paths' && draft.artifact.payload.mode === 'entire_archive')
    draft.artifact.payload.include = clone(modeCache.payloadInclude || []);
  let parent = draft;
  for (const part of parts.slice(0, -1)) {
    if (parent?.[part] === undefined) throw new Error(`Missing Recipe path: ${path}`);
    parent = parent[part];
  }
  if (parent === null || typeof parent !== 'object') throw new Error(`Missing Recipe path: ${path}`);
  parent[parts.at(-1)] = clone(value);
  return {...editor, draft, modeCache};
}
export function remove(editor, path) {
  if (!ownership(path,editor.managed,editor.editablePaths).startsWith('EDITABLE_')) throw new Error(`Recipe field is read-only: ${path}`);
  const parts = segments(path), draft = clone(editor.draft);
  const parent = readPath(draft, parts.slice(0, -1).join('.'));
  if (Array.isArray(parent)) parent.splice(Number(parts.at(-1)), 1);
  else if (parent && typeof parent === 'object') delete parent[parts.at(-1)];
  else throw new Error(`Missing Recipe path: ${path}`);
  return {...editor, draft};
}
export function move(editor, path, from, to) {
  const rows = readPath(editor.draft, path);
  if (!Array.isArray(rows) || from < 0 || to < 0 || from >= rows.length || to >= rows.length) return editor;
  const next = clone(rows);
  next.splice(to, 0, next.splice(from, 1)[0]);
  return change(editor, path, next);
}
function diffPaths(a, b, prefix) {
  if (equal(a, b)) return [];
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return [prefix];
    return a.flatMap((value, index) => diffPaths(value, b[index], `${prefix}[${index}]`));
  }
  if (a && b && typeof a === 'object' && typeof b === 'object')
    return [...new Set([...Object.keys(a), ...Object.keys(b)])].flatMap(key => diffPaths(a[key], b[key], `${prefix}.${key}`));
  return [prefix];
}
export function changedPaths(editor, a = editor.baseline, b = editor.draft, prefix = '$') {
  return diffPaths(a,b,prefix);
}

export function validationErrors(error) {
  const path = error?.path || error?.details?.path || '$';
  const section = segments(path)[0] || 'global';
  return {global: section === 'global' ? [error] : [], sections: {[section]: [error]}, fields: {[path]: [error]},
    items: /\[\d+\]/.test(path) ? {[path]: [error]} : {}};
}
