// A check mark is reserved for a completed successful milestone.
const failure = new Set(['failed','error','blocked','build_failed','validation_failed','publication_failed']);
const attention = new Set(['warning','validation_needed','ready_to_validate','ready_to_publish','publication_available','update_available','action_required','build_required','recipe_missing']);
const active = new Set(['running','queued','cancelling']);
const ready = new Set(['ready','prepared','ok','healthy','available','up_to_date']);
const complete = new Set(['success','completed','validated','published']);

export function statusSemantics(value) {
  const state = String(value || '').toLowerCase();
  if (failure.has(state)) return {tone:'danger',icon:'!'};
  if (attention.has(state)) return {tone:'warning',icon:'●'};
  if (active.has(state)) return {tone:'info',icon:'◌'};
  if (ready.has(state)) return {tone:'ready',icon:'●'};
  if (complete.has(state)) return {tone:'success',icon:'✓'};
  return {tone:'neutral',icon:'●'};
}

const activeStates = new Set(['pending','queued','running','building','validating','publishing','cancelling']);
const stoppedStates = new Set(['cancelled','validation_cancelled']);

export function operatorStatusSemantics(value) {
  const state = String(value || '').toLowerCase();
  if (state === 'update_available') return {tone:'update',icon:'●'};
  if (activeStates.has(state)) return {tone:'info',icon:'◌'};
  if (stoppedStates.has(state)) return {tone:'neutral',icon:'–'};
  if (state === 'up_to_date') return {tone:'success',icon:'●'};
  return statusSemantics(state);
}
