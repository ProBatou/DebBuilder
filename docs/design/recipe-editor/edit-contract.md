# #24C2B Recipe editor contract

Authority is `debbuilder/recipe_schema.py` v5 and `recipe_document_for_storage()`.
`recipe_store.py` returns the compact persisted representation. The old
`static/recipe_serialization.js` reconstructs authored data from form state;
the new editor does not use it or inherit its defaults. `static/` still serves
production.

## Draft and ownership

`frontend/src/features/recipes/draft.js` deep clones the canonical GET body as
an immutable baseline and a separate draft. The serializer clones the draft.
No form, locale, theme, or section navigation manufactures Recipe fields.
Structural comparison ignores object member order but retains array order,
types, absent keys, null, and empty values. Cancel clones the baseline again.
The canonical fixture corpus must compare exactly after untouched hydration.

`fields.js` is the UI ownership map. It assigns explicit paths to Plan,
Customize, Advanced, and Expert. `name`, schema version, service configured,
detection fields, and management metadata are read only or derived. Other
canonical v5 paths are `PRESERVE_UNOWNED`; their values stay in the draft,
including hidden sections. This is a v5 guarantee, not support for unknown
future fields. Conditional source, build, service, and ELF controls preserve
inactive data. Mode switches handle schema-required dependent fields and keep
an editor-only cache so switching back restores the draft. Backend validation
remains authoritative for cross-field completeness.

Managed self-build remains under System. `/api/workflows` supplies
`editable_paths`; the frontend enforces those paths in controls and the draft
mutation boundary. Backend `builtin_recipe.py` retains the closed allowlist.

## Local actions

Edit, Validate, Cancel, and Review changes are local editor actions. Review
shows the loaded baseline and current draft, including canonical JSON. Dirty
state is structural difference from baseline. Dirty navigation prompts in an
application dialog; reload/close uses `beforeunload` only while dirty. Cancel
makes no request. The only Svelte Recipe POST is `/api/recipes/validate`, with
the current candidate. The UI preserves backend error text and path, maps the
path to the nearest section/control, and keeps the draft on 401, 403, 422, or
network failure. Validation is ephemeral and does not mark the draft saved.

## Create, rename, Test, import

The backend constructs v5 by canonicalizing a supplied name and optional
fields; `normalize_recipe()` defines its defaults. Legacy Create starts with
browser form values. There is no dedicated canonical new-Recipe baseline API,
so C2B does not expose Create. C2C must specify initial identity/source
choices and obtain a canonical validated baseline before persistence.

`name` is Recipe ID and store filename. Current save accepts `previous_id`,
writes the destination, then deletes the previous user file and updates package
associations. Destination collisions can overwrite a user Recipe; automation
state is keyed by identity. Managed identity cannot be renamed. `name` is read
only in C2B. A safe rename transaction and collision policy are needed first.

`POST /api/run` accepts a full Recipe body plus `dry_run`; legacy Test and
Build use the current form candidate, including edits that may not have
finished autosaving. C2C should validate and submit the exact current draft
for Test/Build, explain unsaved state, and never imply that a Run saves a Recipe.
No C2B Run call exists.

Export is the canonical GET/JSON review. Future Import should parse locally,
validate, review the canonical result and `collision` projection, then require
an explicit replacement decision before durable import. No Import POST is
wired in C2B.

## Future save precondition

`GET /api/workflows/{id}` returns an `ETag` with SHA-256 of exact persisted
Recipe bytes, read under the Recipe lease with the body. Body shape is
unchanged. A future editor can send the unquoted lowercase digest as
`expected_revision` alongside `workflow` in the existing save wrapper. The
store compares it under the same process/thread lease used for atomic write.
A missing or changed target returns 409 `recipe_revision_conflict` without
overwrite. Invalid digests return 422 `invalid_recipe_revision`. Omitting the
field preserves legacy save behavior. Managed Recipes still enforce the
backend allowlist. Revision preconditions do not support rename. C2C should
reload/review on 409 and retry only after user resolution with a fresh
revision. Svelte does not send this save request in C2B.
