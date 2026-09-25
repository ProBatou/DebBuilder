import {api} from '../api/client.js';
import {navigate} from './location.js';
import {isManaged} from '../features/recipes/model.js';

export async function openRecipe(recipeId, opts = {}) {
  const canonical = await api.workflow(recipeId, opts);
  const managed = isManaged(canonical);
  navigate(managed ? 'system' : 'recipes', managed ? 'managed' : recipeId);
  return canonical;
}
