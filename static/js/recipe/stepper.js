const RECIPE_STEP_IDS = ['source', 'build', 'install', 'service'];
let recipeStepFrame = null;

function setActiveRecipeStep(step) {
  document.querySelectorAll('.recipe-stepper a[data-recipe-step]').forEach(link => {
    const active = link.dataset.recipeStep === step;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'step');
    else link.removeAttribute('aria-current');
  });
}

function recipeStickyOffset() {
  let offset = 24;
  ['.mobile-topbar', '.recipe-stepper', '.recipe-simple-toolbar'].forEach(selector => {
    const node = document.querySelector(selector);
    if (!node) return;
    const style = getComputedStyle(node);
    if (!['fixed', 'sticky'].includes(style.position)) return;
    const rect = node.getBoundingClientRect();
    if (rect.height > 0) offset += rect.height;
  });
  return offset;
}

function visibleRecipeSections() {
  return RECIPE_STEP_IDS.map(step => ({step, node: $(`recipe-step-${step}`)})).filter(({node}) => {
    if (!node || node.hidden || node.classList.contains('not-applicable')) return false;
    return getComputedStyle(node).display !== 'none';
  });
}

function updateActiveRecipeStep() {
  recipeStepFrame = null;
  if (!$('view-recipes')?.classList.contains('active')) return;
  const sections = visibleRecipeSections();
  if (!sections.length) return;
  const threshold = recipeStickyOffset();
  let active = sections[0];
  sections.forEach(section => {
    if (section.node.getBoundingClientRect().top <= threshold) active = section;
  });
  setActiveRecipeStep(active.step);
}

function scheduleRecipeStepUpdate() {
  if (recipeStepFrame !== null) return;
  recipeStepFrame = requestAnimationFrame(updateActiveRecipeStep);
}

function initRecipeStepper() {
  document.querySelectorAll('.recipe-stepper a[data-recipe-step]').forEach(link => {
    link.addEventListener('click', () => setActiveRecipeStep(link.dataset.recipeStep));
  });
  document.querySelector('.content')?.addEventListener('scroll', scheduleRecipeStepUpdate, {passive: true});
  window.addEventListener('scroll', scheduleRecipeStepUpdate, {passive: true});
  window.addEventListener('resize', scheduleRecipeStepUpdate);
  scheduleRecipeStepUpdate();
}
