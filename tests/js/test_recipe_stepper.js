const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function classList(initial = []) {
  const values = new Set(initial);
  return {
    add: value => values.add(value),
    remove: value => values.delete(value),
    toggle: (value, enabled) => enabled ? values.add(value) : values.delete(value),
    contains: value => values.has(value),
  };
}

function link(step) {
  const attributes = new Map();
  return {
    classList: classList(),
    dataset: {recipeStep: step},
    setAttribute: (name, value) => attributes.set(name, value),
    removeAttribute: name => attributes.delete(name),
    getAttribute: name => attributes.get(name),
  };
}

function section(top) {
  return {
    classList: classList(),
    hidden: false,
    top,
    getBoundingClientRect() { return {top: this.top, height: 400}; },
  };
}

const links = ['source', 'build', 'install', 'service'].map(link);
const sections = {
  source: section(-800),
  build: section(-80),
  install: section(260),
  service: section(900),
};
const view = {classList: classList(['active'])};
const sticky = {
  '.mobile-topbar': {position: 'none', height: 0},
  '.recipe-stepper': {position: 'sticky', height: 42},
  '.recipe-simple-toolbar': {position: 'sticky', height: 46},
};
const stickyNodes = Object.fromEntries(Object.entries(sticky).map(([selector, value]) => [selector, {
  stylePosition: value.position,
  getBoundingClientRect: () => ({top: 0, height: value.height}),
}]));

const context = vm.createContext({
  document: {
    querySelector: selector => stickyNodes[selector] || null,
    querySelectorAll: selector => selector === '.recipe-stepper a[data-recipe-step]' ? links : [],
  },
  getComputedStyle: node => ({position: node.stylePosition || 'static', display: 'block'}),
  requestAnimationFrame: callback => { callback(); return 1; },
  window: {addEventListener: () => {}},
  $: id => id === 'view-recipes' ? view : sections[id.replace('recipe-step-', '')] || null,
});

vm.runInContext(fs.readFileSync('static/js/recipe/stepper.js', 'utf8'), context, {filename: 'stepper.js'});
context.updateActiveRecipeStep();
assert.equal(links[1].classList.contains('active'), true);
assert.equal(links[1].getAttribute('aria-current'), 'step');
assert.equal(links[0].getAttribute('aria-current'), undefined);

sections.build.top = 100;
context.updateActiveRecipeStep();
assert.equal(links[1].classList.contains('active'), true);
assert.equal(links[1].getAttribute('aria-current'), 'step');

sections.build.classList.add('not-applicable');
sections.install.top = 70;
context.updateActiveRecipeStep();
assert.equal(links[2].classList.contains('active'), true);
assert.equal(links[2].getAttribute('aria-current'), 'step');
assert.equal(links[1].getAttribute('aria-current'), undefined);
