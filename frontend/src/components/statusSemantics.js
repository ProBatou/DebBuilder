// A check mark is reserved for a completed successful milestone.
const failure = new Set(['failed','error','blocked','build_failed','validation_failed','publication_failed']);
const attention = new Set(['warning','validation_needed','update_available','action_required']);
const active = new Set(['running','queued','cancelling']);
const ready = new Set(['ready','prepared','ready_to_validate','ready_to_publish','ok','healthy','available','up_to_date']);
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
