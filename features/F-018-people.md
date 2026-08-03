# F-018 — People (Faces, Persons, Naming & Account Links)

**Status:** Deferred (fully specified — wanted post-v1; everything here is additive, so v1 ships nothing for it and precludes nothing)
**Priority:** P2
**Clients:** all
**Depends on:** [F-005](F-005-image-analysis.md) (image pipeline; the F-005 out-of-scope promise this feature redeems), [F-006](F-006-av-transcription-and-keyframes.md) (keyframe chaining → faces in video), [F-002](F-002-hybrid-search.md) (filters, facets, anchors), [F-009](F-009-reprocessing.md) (generations), [F-008](F-008-sharing-and-public-links.md) (grants govern visibility)
**Related specs:** [ADR-0011](../decisions/ADR-0011-person-recognition-architecture.md) (the shape of the whole feature), [ADR-0004](../decisions/ADR-0004-tag-provenance-and-reprocessing.md) (provenance model, reused verbatim), [02-domain-model](../specs/02-domain-model.md#person--faceinstance--personappearance-f-018--deferred), [04-ingestion-pipeline](../specs/04-ingestion-pipeline.md), [05-extractor-contract](../specs/05-extractor-contract.md) (`faces` output kind), [06-search](../specs/06-search.md), [07-identity-permissions-sharing](../specs/07-identity-permissions-sharing.md#persons--face-data-f-018--deferred), [09-previews](../specs/09-previews.md) (face crops)

## Summary

Photos and video keyframes are scanned for **faces** by an opt-in local extractor (`face-detect`): bounding boxes, quality scores, small crops, and embeddings in a dedicated `face-v1` space. Core-owned **identity resolution** clusters those faces into **persons owned by the workspace owner** — never instance-global, never across users ([ADR-0011](../decisions/ADR-0011-person-recognition-architecture.md)). Users name persons, merge clusters, correct mistakes (corrections stick across model upgrades — the [ADR-0004](../decisions/ADR-0004-tag-provenance-and-reprocessing.md) state machine applied to faces), search and facet by person with positional anchors ("video Y, Anna at 04:12"), and may link a person to an instance account, which powers `persons: me`. Nothing runs and nothing is exposed without the two-level enablement gate below; erasure is a first-class operation.

## Enablement model

Face analysis is **off until deliberately enabled**, at two levels:

- **Instance setting** (admin): `disabled | default_off | default_on` — fresh installs start at `default_off`. `disabled` is the hard off-switch: no face jobs anywhere, workspace settings inert.
- **Workspace setting** (workspace owner): `face_recognition: inherit | on | off` — default `inherit` (follows the instance default).

A file's face processing is **effectively enabled** iff the instance setting is not `disabled` and its workspace resolves to `on` (explicit `on`, or `inherit` under `default_on`). Disabling **suspends** (stops processing, hides everything, keeps data at rest — re-enabling is cheap); **purging** is a separate, explicit, owner-triggered act (FR-6). This gate protects the *owner's* choice about analyzing their own library; the people photographed cannot be asked by any mechanism — operator guidance is [Q52](../OPEN-QUESTIONS.md).

## User stories

- As a user, I want the faces in my photos grouped automatically so that naming a person once makes their whole history findable ("all photos of Anna" — and inside videos, at the right timestamp).
- As a user, I want to correct the machine — "this is not Anna" — and have that stick forever, including across model upgrades.
- As a user, I want face recognition off for my scanned-documents workspace but on for my photo library, and a way to erase all face data on demand.
- As a family member with access to our shared workspace, I want to see the names its owner assigned — one shared truth per file, like tags.
- As a user whose face appears in a relative's workspace, I want "photos of me" to work across everything shared with me, via my account — and I want to see and remove any link tying my account to a face cluster.
- As a user, I want "group photos" (3+ faces) findable without ever naming anyone.

## Functional requirements

Enablement & erasure:

- **FR-1** The instance carries a face-recognition setting `disabled | default_off | default_on`, readable by every member, changeable only by admins, `default_off` on fresh install; every change is event-logged ([ADR-0007](../decisions/ADR-0007-unified-event-log.md)).
- **FR-2** Every workspace carries `face_recognition: inherit | on | off` (default `inherit`), readable and changeable only by the workspace owner (admins get no override — [07](../specs/07-identity-permissions-sharing.md#persons--face-data-f-018--deferred)); every change is event-logged.
- **FR-3** *(negative space)* For files whose workspace is not [effectively enabled](#enablement-model): no face-analysis or identity-resolution job is ever created, and no face instance, face embedding, face crop, person appearance, or `face_count` entry derived from those files is returned by any API surface — file detail, `/files/{id}/faces`, search results, facets, `why` signals, `/people` listings, counts, and cover thumbnails included.
- **FR-4** A workspace becoming effectively enabled queues face analysis for its live file versions at searchability priority (P2 — [04](../specs/04-ingestion-pipeline.md#prioritization--scheduling)); versions whose content hash was already analyzed with the current extractor + model version reuse those results ([04 §2](../specs/04-ingestion-pipeline.md#2-identification)) instead of recomputing.
- **FR-5** A workspace ceasing to be effectively enabled stops new analysis and suspends exposure (FR-3) without deleting stored face data; the retained volume (instance count, crop bytes) is reported to the workspace owner; re-enabling restores exposure without recomputation.
- **FR-6** The workspace owner can **purge** a workspace's face data: every face instance, face embedding, and face crop derived from the workspace's file versions and every person appearance on its files is deleted (rows and stored bytes — [02 § invariant 8](../specs/02-domain-model.md#invariants)); persons of that owner left with no appearance and no instance anywhere are deleted; afterwards no API surface returns any of it and re-enabling recomputes from scratch. The purge itself is event-logged; the event log is the only remaining trace.
- **FR-7** Moving a file applies the destination workspace's effective enablement immediately: into a not-effectively-enabled workspace → the file's face data is suspended (FR-3 semantics); into an effectively enabled one → exposure resumes, and analysis is queued unless current-version face data already exists.

Detection (extractor):

- **FR-8** For every image file version and every video keyframe under effective enablement, the `face-detect` extractor produces zero or more **face instances**: normalized bounding box, detection-quality score (0–1), an embedding in the dedicated `face-v1` space computed from the face region only, and a face-crop derived asset ([09](../specs/09-previews.md#storage)); keyframe-sourced instances carry the keyframe timestamp. Every row is provenance-stamped ([02 § invariant 3](../specs/02-domain-model.md#invariants)).
- **FR-9** *(negative space)* `face-v1` vectors are never compared against any other embedding space, and free-text queries never target `face-v1`: no text query produces a face-space match ([06 § embedding spaces](../specs/06-search.md#embedding-spaces-never-mixed)).
- **FR-10** Each analyzed image version carries the well-known integer metadata key `face_count` (detected instances, including 0), filterable by equality and range like any integer metadata ([F-002/FR-2](F-002-hybrid-search.md)).
- **FR-11** All face models run locally inside the extractor container (`network: none` — [05](../specs/05-extractor-contract.md#container-requirements-hardening)); the extractor is CPU-only capable and uses a GPU when present.

Identity resolution (core):

- **FR-12** Core identity resolution assigns each new face instance to a person **owned by the file's workspace owner** when its similarity to that person's exemplar faces meets the auto-assign threshold ([Q51](../OPEN-QUESTIONS.md)), creating an `auto` appearance whose confidence is the similarity score.
- **FR-13** Instances assigned to no person, at or above the detection-quality floor, are clustered into new **unnamed persons** of the same owner once a minimum cluster size is reached ([Q51](../OPEN-QUESTIONS.md)); instances below floor or cluster size remain unassigned, retrievable per file via `/files/{id}/faces`.
- **FR-14** *(negative space)* Identity resolution never crosses owners: no matching, exemplar sharing, cluster membership, or suggestion ever links a face instance in one user's workspaces to a person owned by another user — under any setting combination.
- **FR-15** Appearances carry provenance `manual | auto | confirmed | rejected` with [ADR-0004](../decisions/ADR-0004-tag-provenance-and-reprocessing.md) semantics and source stamping: confirming makes an `auto` appearance user truth; rejecting ("this is not X") is a negative record — no future generation, threshold change, or re-cluster may re-create an `auto` appearance for that (file, person) pair.
- **FR-16** Reprocessing with a new extractor/model version swaps a file's face instances atomically per file as one generation ([F-009/FR-4](F-009-reprocessing.md)); `auto` appearances are re-derived; `manual`/`confirmed` appearances persist — re-anchored to the new-generation instance with the highest bounding-box IoU where that IoU is ≥ 0.5, retained without an anchor otherwise.
- **FR-17** *(negative space)* An appearance only ever references a person owned by the file's **current** workspace owner. A move that changes the owner deletes the previous owner's appearances on that file (event-logged) and queues identity resolution for the new owner; at no point does an appearance reference a foreign owner's person.

Curation:

- **FR-18** The person owner can set, change, and remove a person's display name, and hide/unhide a person; hidden persons disappear from default `/people` listings and from facets but remain filterable by id.
- **FR-19** The person owner can merge persons they own into one: appearances and instance assignments move to the target, the sources are deleted, and the merge is event-logged with source and target ids.
- **FR-20** The person owner can delete a person: the person and all its appearances are removed; its face instances return to the unassigned pool and are not re-clustered into a new person unless clustering criteria are met again by future resolution runs.
- **FR-21** A user with `write` on a file can curate that file's appearances: assign or reassign a face instance to a person of the file's owner that is visible to the caller (FR-26), confirm an `auto` appearance, reject an appearance — each recorded with the acting user id ([F-003/FR-2](F-003-tagging.md) pattern).
- **FR-22** *(negative space)* No operation available to a non-owner creates, renames, merges, hides, deletes, or account-links a person of another user; FR-21 assignment can only target existing persons, and an id the caller cannot observe (FR-26) is handled identically to a nonexistent id.
- **FR-23** A user with `write` can add and remove a **manual appearance without a face instance** ("person P is in file F" — e.g. a face the detector missed): `manual` provenance, immune to reprocessing (untouched by FR-16), returned by search, facets, and listings like any appearance, carrying no anchor.

Search:

- **FR-24** `POST /search` accepts a `persons` filter (list of person ids and/or `me`): a file matches iff it satisfies **every** entry, an id-entry being satisfied by a non-`rejected` appearance of that person. The filter combines with every mode, filter, sort, and aggregation of [F-002](F-002-hybrid-search.md) and is enforced inside every query branch including ANN, like [F-002/FR-7](F-002-hybrid-search.md) and [FR-13](F-002-hybrid-search.md).
- **FR-25** Search facets include `persons` — id, display name, count — over **named**, non-hidden persons only; facet presence and counts obey [F-002/FR-7](F-002-hybrid-search.md) and [FR-13](F-002-hybrid-search.md) exactly like tag facets.
- **FR-26** *(negative space)* A caller can observe a person — in listings, direct `GET`, thumbnails, facets, or filter behavior — iff they own it **or** can read at least one live file, in an effectively enabled workspace, carrying a non-`rejected` appearance of it. Any other person id answers `404`, indistinguishable from one that never existed ([08 § errors](../specs/08-api-principles.md#errors-rfc-9457)); filtering by it behaves identically to filtering by a nonexistent id.
- **FR-27** Person search hits carry match entries with anchors — image region; timestamp (+ region) for keyframe-derived instances — in the [F-002/FR-5](F-002-hybrid-search.md) result shape; the `why` list ([F-002/FR-6](F-002-hybrid-search.md)) includes the person signal with provenance and confidence.
- **FR-28** *(negative space)* Share-link responses ([F-008/FR-5](F-008-sharing-and-public-links.md)) contain no person or face data: no person names or ids, no boxes, no crops, no `face_count`, no face-derived fields — in the descriptor, preview assets, and every other byte served under the token.

People listing & accounts:

- **FR-29** `GET /people` lists the persons visible to the caller (FR-26) with display name, face and file counts computed over the caller's readable live files in effectively enabled workspaces, cover-crop reference, linked account where present, and (for the owner) hidden state; unnamed persons are listed only to their owner; pagination is cursor-based ([08](../specs/08-api-principles.md#conventions-proposed)).
- **FR-30** *(negative space)* `GET /people/{id}/thumbnail` serves a face crop sourced from a file version the **caller** can read: the owner's chosen cover when that file is readable by the caller, otherwise a deterministic fallback crop from the caller's readable files — never bytes from a file the caller cannot read.
- **FR-31** The person owner can link a person to an instance account and remove that link; the linked user can list every person linked to their account (across owners) and remove any such link themselves; every link and unlink is event-logged and attributed.
- **FR-32** The `persons` filter value `me` resolves to the set of persons linked to the caller's account across all owners; it is satisfied by a non-`rejected` appearance of **any** of them (they represent one human), then combines with other entries per FR-24. An empty set yields zero results, not an error.
- **FR-33** Every state change in this feature — settings (FR-1–2), purge, person create/rename/hide/merge/delete, appearance create/assign/confirm/reject/remove, account link/unlink — is event-logged in the same transaction ([ADR-0007](../decisions/ADR-0007-unified-event-log.md)) and emits [F-012](F-012-live-updates.md) notifications, so open people surfaces refresh live.

## API surface

```
GET    /people                        visible persons (FR-26, FR-29); ?linked_account=me → persons
                                      linked to the caller across owners (FR-31)
GET    /people/{id}                   detail: name, counts, cover ref, linked account, hidden (owner)
PATCH  /people/{id}                   owner: rename · hide/unhide · link/unlink account
POST   /people/{id}/merge             owner: merge listed own persons into {id}
DELETE /people/{id}                   owner: delete person (FR-20)
GET    /people/{id}/thumbnail         cover face crop, caller-readable source only (FR-30)
GET    /files/{id}/faces              face instances + appearances of a file (read)
POST   /files/{id}/faces/{fid}/assign write: assign/reassign an instance to a person (FR-21)
POST   /files/{id}/people             write: manual appearance without instance (FR-23)
POST   /files/{id}/people/{pid}/confirm · …/reject · DELETE /files/{id}/people/{pid}   (FR-21)
POST   /workspaces/{ws}/face-data/purge   owner, explicit (FR-6)
```

The workspace setting rides `PATCH /workspaces/{ws}` (FR-2); the instance setting joins the admin settings surface (exact endpoint decided with the instance-settings home, sketch-level while Deferred). `POST /search` gains the `persons` filter and facet ([06](../specs/06-search.md#filters-composable-with-any-mode)). A **People** overview page is a dedicated page, not a view (rows are person entities — [F-017 § What is a view](F-017-views.md#what-is-a-view--and-what-is-not)) and joins the reserved navigation entries ([F-017/FR-7](F-017-views.md)); "all files of person X" is an ordinary search and therefore view-able.

## Out of scope

- **User-drawn face regions** (manual boxes for missed faces): additive later via extractor job `params` ("embed these regions"); v1 of this feature covers the miss with anchor-less manual appearances (FR-23).
- **Pet/animal recognition** — face models are human-specific; a future extractor with its own embedding space.
- **Free-text name resolution** ("Anna at the beach" as raw query text): clients offer person chips/pickers; query understanding is a later concern. `face-v1` stays matching-only (FR-9).
- **Cross-user person sharing or merging** — account links (FR-31) are the only cross-owner join, by design ([ADR-0011](../decisions/ADR-0011-person-recognition-architecture.md)).
- **Face-based automation** (auto-share "photos of me", new-appearance notifications — [Q37](../OPEN-QUESTIONS.md)-class standing queries).
- **Face-aware smart thumbnail cropping** — [09](../specs/09-previews.md#thumbnails) already notes it as possible-later; face boxes would supply the regions.

## Open questions

[Q50 (face model choices & licensing)](../OPEN-QUESTIONS.md) · [Q51 (identity-resolution tuning & evaluation corpus)](../OPEN-QUESTIONS.md) · [Q52 (erasure granularity & retention guidance)](../OPEN-QUESTIONS.md) · [Q53 (account-link consent)](../OPEN-QUESTIONS.md) · [Q5](../OPEN-QUESTIONS.md) gates the extractor wire mechanics exactly as for the other extractors.

## Acceptance criteria

- **AC-1** (FR-2, FR-4, FR-8, FR-12–13, FR-29) Enabling `face_recognition: on` on a workspace of 500 family photos queues analysis; recurring faces form unnamed persons at the cluster threshold; the owner names one "Anna"; `GET /people` lists Anna with face/file counts; a newly uploaded photo of Anna gains an `auto` appearance carrying a similarity confidence.
- **AC-2** (FR-24, FR-25, FR-27) `persons: [anna]` + `class: image` returns exactly her files, each hit with region anchors and a `why` entry like `person:anna(auto,0.83)`; the facet lists Anna with the correct count.
- **AC-3** (FR-15, FR-16) The owner rejects Anna on one photo and confirms her on another; after reprocessing with a newer face model, the rejected appearance is absent, the confirmed one persists and is anchored to the new generation's matching box; a confirmed appearance whose face the new model no longer detects persists without an anchor.
- **AC-4** (FR-26, FR-25) Bob without grants: every person id of Alice answers `404`; his searches return zero person facets from her data. After a `read` grant on one folder, Bob sees Anna with counts covering only that folder; after revocation, `404` again — verified with [F-002/FR-7](F-002-hybrid-search.md) leak rigor.
- **AC-5** (FR-8, FR-27) A person visible at ~04:12 in a video yields a keyframe-derived hit whose timestamp anchor is within ±5 s.
- **AC-6** (FR-3, FR-5, FR-6) Disabling the workspace removes all its face data from every surface while the owner sees the retained volume; purging empties `/files/{id}/faces`, deletes the crops from the derived store, and deletes a person whose only support it was; re-enabling after purge recomputes.
- **AC-7** (FR-28) A share link to a photo with named persons serves a response tree (descriptor + preview assets) containing zero person or face fields — asserted by schema diff against the same file's authenticated responses.
- **AC-8** (FR-31, FR-32) Alice links her person "Tom" to account @tom; Bob links his "Tommy" to @tom too; Tom's `persons: [me]` search returns the union of both owners' matching files he can read; Tom self-unlinks from Alice's person and her files drop out of `me` immediately.
- **AC-9** (FR-21, FR-22) Bob (`write` on Alice's shared photo) assigns an unassigned face to Alice's person Anna (visible to him) — succeeds and is stamped with Bob's id; Bob renaming, merging, deleting, or linking Alice's persons fails; assigning to a person id he cannot observe fails identically to a nonexistent id.
- **AC-10** (FR-7, FR-17) Moving a photo into Alice's disabled workspace suspends its face data everywhere; moving a photo from Alice's to Bob's (enabled) workspace removes Alice-person appearances (event-logged) and produces Bob-side auto appearances on the next resolution run.
- **AC-11** (FR-23) On a photo where the detector missed a profile face, a writer adds a manual "Anna appears here" appearance: the file matches `persons: [anna]`, the appearance shows `manual` provenance and no anchor, and it survives a full reprocess byte-identically.
- **AC-12** (FR-10) `face_count >= 3` + `class: image` lists group photos, including photos containing only never-named people — and returns nothing from workspaces where face recognition is off (FR-3).
- **AC-13** (FR-30) Anna's owner sets a cover crop from a private file; Bob (who reads only one folder with Anna) fetches `GET /people/{anna}/thumbnail` and receives a crop sourced from a file he can read — never the private cover.
