## v0.2.0 (2026-08-22)

### Feat

- **traceability**: read the browser suites, and close out phase 1
- **deploy**: let the files live on your own storage, not only in a volume
- **web**: the app itself — served from the API's own origin, behind its own login
- **folders**: recognise a renamed directory as the folder it already was
- **folders**: report what a folder holds, without making uploads wait for it
- **folders**: create, rename and move folders without losing what hangs off them
- **scan**: watch workspace roots, so an external change shows up in seconds
- **scan**: reconcile external changes, and keep every overwritten version
- **scan**: a manual rescan records who asked, and cannot delay a due scan
- **scan**: import an existing tree, and scan it again on a schedule
- **upload**: the resumable-upload protocol, file registration and download
- **storage**: workspaces in both placements, the control directory and the folder tree
- **storage**: the shared write layer, blob store, probe, janitor and audit
- **operations**: durable intent, leases and fencing for every effectful operation

### Fix

- **ci**: build the image from the repository root, where the web client is

## v0.1.0 (2026-08-20)

### Feat

- **deploy**: ship the compose stack, install guide and release tooling
- **tools**: compute requirement traceability and lint the specifications
- **api,web**: commit the OpenAPI contract and scaffold the Vue web client
- **server**: core service skeleton with health, migrations and deny-by-default API

### Fix

- **release**: keep the lockfile and the contract stable across a version bump
- **build**: remove pip from the runtime image
