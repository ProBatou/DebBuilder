import {ApiError, request} from '../../api/client.js';
import {candidate} from './draft.js';
import {validateRecipe} from './persistence.js';

function deepFreeze(value) {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

// Keep one private snapshot across validation and admission while the editor
// remains available for further local changes.
export async function admitDraftRun(editor, dryRun, {onStarting, ...opts} = {}) {
  const snapshot = deepFreeze(candidate(editor));
  await validateRecipe(snapshot, opts);
  onStarting?.();
  const result = await request('/api/run', {
    ...opts, method: 'POST', body: {workflow: snapshot, dry_run: dryRun},
  });
  if (typeof result?.run_id !== 'string' || !result.run_id || result.status !== 'queued') {
    throw new ApiError({code:'admission_response_invalid',message:'Run admission response has no queued Run ID.',kind:'ambiguous'});
  }
  return result.run_id;
}
