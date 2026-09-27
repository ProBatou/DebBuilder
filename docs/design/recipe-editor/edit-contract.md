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
Build use the current form candidate. C2D validates and submits one cloned
current draft for either action without saving it. Test is `dry_run: true`:
source resolution/acquisition, detection, dependency checks, source changes,
build planning, and staging preview run when applicable; build commands and
final package creation do not. A successful Test ends `prepared`. Build is
`dry_run: false`, executes the pipeline and produces a `.deb` or selected
upstream artifact on `success`. Artifact Validation and Publication remain
separate lifecycle stages, but existing server settings can start them after
a successful Build. C2D sends no lifecycle mutation requests. Never-saved
Create cannot run until its first Save. An inactive
current draft cannot run, but unsaved edits to an existing active Recipe can.
The admitted Run ID comes only from the 202 response. View Run uses normal
dirty-navigation protection. A network/timeout outcome is ambiguous: check
Runs before manually trying again; no admission request is retried.

The Recipe ETag hashes exact persisted bytes. Run `recipe_sha256` identifies
the immutable canonical runtime snapshot and must not be compared with the
Recipe ETag to claim matching current evidence.

Export is the canonical GET/JSON review. Future Import should parse locally,
validate, review the canonical result and `collision` projection, then require
an explicit replacement decision before durable import. No Import POST is
wired in C2B.

## #24C2C durable persistence

Existing Svelte edits retain the canonical GET body and strong ETag digest as
baseline and revision. Save freezes the draft, validates that exact candidate
through `/api/recipes/validate`, then posts it with `expected_revision`. The
store checks under its Recipe lease and returns the canonical saved Recipe and
fresh revision from the same write. Save fields are disabled during this short
validate/write sequence; Cancel and navigation are blocked until it finishes.
The acknowledged canonical Recipe becomes the new baseline. Later Cancel resets
to this baseline. No autosave is used.

A 409 revision conflict keeps the local draft and original revision. The editor
fetches the latest server Recipe and ETag without installing them, and shows
paths changed locally, on the server, or both. Keep editing retains the warning
and blocks Save. Discard/reload installs the server body and revision. There is
no force overwrite or automatic merge. Network, authorization, and validation
failures retain the draft; backend path and prose remain visible.

Create starts with Recipe ID and GitHub repository. `POST /api/recipes/draft`
projects a canonical v5 baseline from `recipe_document_for_storage()` without
persistence. First Save uses `create_only: true`, checked atomically under the
store lease. A collision preserves the draft and allows changing its ID; no
Package is created by guarded Save. Success changes Create into Edit and
installs the saved canonical body and revision. Managed self-build under System
uses the same guarded Save; the backend allowlist and definition consistency
checks remain authoritative.

Rename is deferred post-v1. Current legacy `previous_id` behavior writes the
destination before deleting the source and updates Package references outside
that transition; destination collision, dependent automation identity, and
crash durability need a separate transaction design. Existing ID/name and
managed identity remain read only in Svelte. Delete, Import persistence,
automation mutation, and observation mutation remain out of scope.
