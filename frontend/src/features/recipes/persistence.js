import {request} from '../../api/client.js';

const path = id => `/api/workflows/${encodeURIComponent(id)}`;
export function parseRevision(value) {
  const match = /^"([0-9a-f]{64})"$/.exec(value || '');
  if (!match) throw new Error('Recipe revision unavailable. Reload the Recipe before saving.');
  return match[1];
}
function receipt(result) {
  return {recipe: result.payload.recipe, revision: parseRevision(result.headers.get('ETag'))};
}
export async function loadRecipe(id, opts = {}) {
  const result = await request(path(id), {...opts, withHeaders: true});
  return {recipe: result.payload, revision: parseRevision(result.headers.get('ETag'))};
}
export const loadConflictVersion = loadRecipe;
export function validateRecipe(recipe, opts = {}) {
  return request('/api/recipes/validate', {...opts, method:'POST', body:{recipe}});
}
export async function saveExistingRecipe(id, recipe, revision, opts = {}) {
  if (!/^[0-9a-f]{64}$/.test(revision || '')) throw new Error('Recipe revision unavailable. Reload the Recipe before saving.');
  return receipt(await request(path(id), {...opts, method:'POST', body:{workflow:recipe, expected_revision:revision}, withHeaders:true}));
}
export async function createRecipe(id, recipe, opts = {}) {
  return receipt(await request(path(id), {...opts, method:'POST', body:{workflow:recipe, create_only:true}, withHeaders:true}));
}
export async function newRecipeDraft(name, repository, opts = {}) {
  return (await request('/api/recipes/draft', {...opts, method:'POST', body:{name,repository}})).recipe;
}
