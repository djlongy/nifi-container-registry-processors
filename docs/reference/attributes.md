---
title: Attribute reference
type: reference
status: current
updated: 2026-10-30
---

# Attribute reference

## Webhook headers (read by ListenHTTP, kept as attributes)

ListenHTTP turns them into attributes with HTTP Headers for Attributes `X-.*`.

| Header | Used by | Purpose |
|---|---|---|
| `X-Artifact-Type` | a RouteOnAttribute, if you add one | `container-images-request` marks a transfer request. No Python processor reads it |
| `X-Sha256` | PlanRegistryTransfer | Must equal the sha256 of the body, or the webhook fails |
| `X-CI-Project`, `X-CI-Pipeline-Url`, `X-CI-Commit-Sha`, `X-Image-Count`, `X-Registry` | nothing | Routing and display only. Never used to select or check images |

## PlanRegistryTransfer

Read from the incoming FlowFile (it ignores the content when `registry.image.reference` is set; otherwise the content is a webhook body, checked against `X-Sha256`, Source Registry URL, Allowed Repositories and the registry's digests):

| Attribute | Direction | Value |
|---|---|---|
| `registry.image.reference` | read | `repository:tag`, `repository@sha256:...` or `repository:tag@sha256:...` |
| `transfer.id` | read, written | Correlation id; a UUID is made when absent |
| `registry.layers` | read | Resend only these layer numbers (and the config), such as `2,3`. No manifest is planned. One image only |
| `registry.check` | read | The 8-character check code of the incomplete bulletin. Required with `registry.layers`; a different code fails the FlowFile because the tag has moved |
| `registry.platform` | read | With `registry.layers`: the platform, such as `linux/arm64`, whose layer numbers are meant (an index only) |
| `registry.error` | written on failure | Why the request was refused |
| `planner.job.count` | written | Jobs in the content |

Each job in `$.jobs`: `kind` (`config`, `blob`, `manifest`, `index`), `repository`, `digest`, `size`, `mediaType`, `tag`, `reference`, `layer`, `layers`, `platform`, `check`, `transfer`, `needs`, `owners` (the digests of the manifests or index that need this job, signalled by the high side once it is stored), and for manifests and indexes `body` (base64 of the exact source bytes). The native flow turns them into the attributes `kind`, `repository`, `digest`, `size`, `tag`, `reference`, `layer.n`, `layer.m`, `platform`, `check`, `transfer.id` and `media.type`.

## RouteRegistryTransfer

Troubleshoot a stuck or failed FlowFile in this order: `registry.error`, then `registry.stage` with `invokehttp.status.code`, then the job attributes.

| Attribute | Direction | Value |
|---|---|---|
| `registry.error` | written | Why the last pass failed, retried or waited. Empty after a clean pass. Starts with the same text as the bulletin |
| `registry.stage` | written | The step a native processor is running for this FlowFile: `fetch` (low InvokeHTTP GET), `verify` (low CryptographicHashContent), `upload` (high blob PUT), `manifest-digest` and `manifest-tag` (high manifest PUTs). Empty between steps |
| `invokehttp.status.code`, `invokehttp.java.exception.message` | read | The answer of the native InvokeHTTP. `0` with a Java message means no HTTP answer. `401`, `408`, `429` and `5xx` retry; other codes fail |
| `registry.http.method`, `registry.http.url`, `registry.http.media.type` | written | The request the native InvokeHTTP makes next |
| `registry.auth.header` | written | A scoped Bearer token for that request, or empty. Cleared after every step. Basic credentials never go in an attribute |
| `registry.kind` | read, written on low | `blob`, `config`, `manifest` or `index` |
| `registry.image`, `registry.platform` | read, written on low | The image (`repository:tag`) and platform the job belongs to, as the bulletins name them |
| `registry.repository`, `registry.digest`, `registry.size`, `registry.media.type` | read, written on low | The planned object. Content of another size or digest fails |
| `registry.reference` | read, written on low | The tag (or digest) a manifest is published under |
| `registry.needs`, `registry.layer.count` | read, written on low | A manifest's blobs or an index's manifests, in order, and the layer count |
| `registry.owners` | read, written on low | JSON list of the manifest or index digests that need this object. The high side signals each one through `signal` once the object is in the registry |
| `registry.signal.id`, `registry.signal.next` | written, high | The owner digest Notify signals next (its Release Signal Identifier), and the index of the owner after it |
| `registry.check` | read, written on low | The check code a resend must quote |
| `resp.Docker-Content-Digest` | read | The digest the destination reports for a manifest. A different digest fails. A registry that sends no such header is asked for the stored digest instead. The tag PUT URL and its Bearer token are rebuilt from Registry URL and Target Repository, never read from an attribute |
| `registry.retry.max`, `registry.retries`, `registry.retries.uuid` | written | RetryTransfer's limit, count and owner for errors. After an error the limit is the current count plus `3`. Every successful step, `complete`, `signal` and `waiting` set the count to `0` and blank the owner, so a retry on the low side never fails the high side's Reuse Mode `fail` and a long wait never uses up the retries of a later error |
| `registry.wait.received`, `registry.wait.expected` | written, high | A manifest or index at its last check: dependencies already in the registry (X) of all its distinct dependencies (Y). Y counts the config blob of a manifest. WaitForLayers uses Y as Target Signal Count |
| `registry.wait.missing` | written, high | The dependency digests still awaited, comma separated. Empty once all are in |
| `registry.manifest.digest` | written, high | The digest the manifest or index is published under: the source digest, unchanged by the transfer |
| `registry.wait.retries`, `registry.wait.retries.uuid` | read, high | RetryWait's count and owner across one whole wait. Not reset while the manifest waits; blanked when it leaves the wait |
| `wait.start.timestamp` | written, high | Wait's own, epoch milliseconds. Set to now on every `waiting` so each WaitForLayers visit gets a full Expiration Duration. Never blank: Wait sends a blank value to `failure` |
| `wait.counter.received` | read | Wait copies the Notify counter onto a released manifest |
| `content_SHA-256` | read, low | CryptographicHashContent's result, compared with `registry.digest` |
