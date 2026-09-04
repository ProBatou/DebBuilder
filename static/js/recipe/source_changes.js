const sourceChangeDefinitions = {
  replace: {operation: 'replace', fields: [['path', 'Target file', 'input'], ['search', 'Content to find', 'textarea'], ['content', 'Replace with', 'textarea']]},
  before: {operation: 'insert_before', fields: [['path', 'Target file', 'input'], ['search', 'Matching text to find', 'textarea'], ['content', 'Content to add before', 'textarea']]},
  after: {operation: 'insert_after', fields: [['path', 'Target file', 'input'], ['search', 'Matching text to find', 'textarea'], ['content', 'Content to add after', 'textarea']]},
  remove: {operation: 'remove', fields: [['path', 'Target file', 'input'], ['search', 'Exact content to find and remove', 'textarea']]},
  create: {operation: 'create_file', fields: [['path', 'New file path', 'input'], ['content', 'File contents', 'textarea']]},
  'delete-file': {operation: 'remove_file', fields: [['path', 'Path of the file to delete', 'input']]},
};

let editingSourceChangeIndex = null;

function sourceChoiceForOperation(operation) {
  return Object.keys(sourceChangeDefinitions).find(key => sourceChangeDefinitions[key].operation === operation) || 'replace';
}

function selectSourceChangeType(type, change = {}) {
  const definition = sourceChangeDefinitions[type] || sourceChangeDefinitions.replace;
  document.querySelectorAll('[data-change-type]').forEach(button => button.classList.toggle('active', button.dataset.changeType === type));
  $('sourceChangeDialog').dataset.changeType = type;
  $('sourceChangeFields').innerHTML = definition.fields.map(([key, label, kind]) => `<label><span>${label}</span>${kind === 'textarea' ? `<textarea data-change-field="${key}" rows="3">${esc(change[key] || '')}</textarea>` : `<input data-change-field="${key}" value="${esc(change[key] || '')}">`}</label>`).join('') + '<div class="change-preview single"><span class="recipe-group-title">Result preview</span><p>The validated change will be applied to the isolated source workspace before build commands.</p></div>';
}

function openSourceChangeDialog(type = 'replace', index = null) {
  editingSourceChangeIndex = index;
  const change = index === null ? {} : window.recipeSourceChanges[index];
  selectSourceChangeType(change?.operation ? sourceChoiceForOperation(change.operation) : type, change || {});
  $('sourceChangeDialog').showModal();
}

function confirmSourceChange() {
  const type = $('sourceChangeDialog').dataset.changeType || 'replace';
  const definition = sourceChangeDefinitions[type];
  const change = {operation: definition.operation};
  $('sourceChangeFields').querySelectorAll('[data-change-field]').forEach(field => {
    change[field.dataset.changeField] = field.value;
  });
  if (editingSourceChangeIndex === null) window.recipeSourceChanges.push(change);
  else window.recipeSourceChanges[editingSourceChangeIndex] = change;
  renderSourceChanges();
  scheduleRecipeAutosave();
  $('sourceChangeDialog').close();
}
