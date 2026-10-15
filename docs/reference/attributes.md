---
title: Attribute reference
type: reference
status: current
updated: 2026-10-15
---

# Attribute reference

## Webhook headers (read by ListenHTTP, kept as attributes)

ListenHTTP turns them into attributes with HTTP Headers to receive as Attributes (Regex) `X-.*`.

| Header | Used by | Purpose |
|---|---|---|
| `X-Artifact-Type` | RouteOnAttribute | `container-images-request` routes the webhook to Export |
| `X-Sha256` | ExportContainerImages | Must equal the sha256 of the body, or the webhook fails |
| `X-CI-Project`, `X-CI-Pipeline-Url`, `X-CI-Commit-Sha`, `X-Image-Count`, `X-Registry` | nothing in Export | Routing and display only. Never used to select or check images |

## PlanRegistryTransfer

Read from the incoming FlowFile (it ignores the content when `registry.image.reference` is set; otherwise the content is a webhook body, checked like Export checks it):

| Attribute | Direction | Value |
|---|---|---|
| `registry.image.reference` | read | `repository:tag`, `repository@sha256:...` or `repository:tag@sha256:...` |
| `transfer.id` | read, written | Correlation id; a UUID is made when absent |
| `registry.layers` | read | Resend only these layer numbers (and the config), such as `2,3`. No manifest is planned. One image only |
| `registry.check` | read | The 8-character check code of the incomplete bulletin. Required with `registry.layers`; a different code fails the FlowFile because the tag has moved |
| `registry.platform` | read | With `registry.layers`: the platform, such as `linux/arm64`, whose layer numbers are meant (an index only) |
| `registry.error` | written on failure | Why the request was refused |
| `planner.job.count` | written | Jobs in the content |

Each job in `$.jobs`: `kind` (`config`, `blob`, `manifest`, `index`), `repository`, `digest`, `size`, `mediaType`, `tag`, `reference`, `layer`, `layers`, `platform`, `check`, `transfer`, `needs`, and for manifests and indexes `body` (base64 of the exact source bytes). The native flow turns them into the attributes `kind`, `repository`, `digest`, `size`, `tag`, `reference`, `layer.n`, `layer.m`, `platform`, `check`, `transfer.id` and `media.type`.

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
| `registry.check` | read, written on low | The check code a resend must quote |
| `registry.manifest.tag.url` | written | The tag PUT that follows a committed digest PUT |
| `resp.Docker-Content-Digest` | read | The digest the destination reports for a manifest. A different digest fails |
| `registry.retry.max` | written | Retries RetryFlowFile allows: `3` after an error, unlimited while a manifest waits |
| `registry.wait.since`, `registry.wait.checks`, `registry.wait.next` | written, high | A manifest waiting for its layers: when the wait began (epoch seconds), checks so far, and when the next check is due. Cleared when the manifest moves to `stale` |
| `content_SHA-256` | read, low | CryptographicHashContent's result, compared with `registry.digest` |

## ExportContainerImages

| Attribute | Direction | Value |
|---|---|---|
| `X-Sha256` | read, then replaced | Read from the webhook; on success it is the tar's sha256 |
| `filename`, `Filename` | written | The tar name (default mode) |
| `absolute.path` | written | The Tar Output Directory with a trailing `/` |
| `Content-Type`, `mime.type` | written | `application/octet-stream`, `application/x-tar` (`application/x-ndjson` with Tar Per Image `true`) |
| `X-Artifact-Type`, `X-Artifact-Format` | written | X-Artifact-Type Value, and `tar` |
| `registry.host` | written | Host of Source Registry URL |
| `registry.images` | written | One `repository:tag@digest` per line, in body order |
| `registry.image.count` | written | Number of images |
| `registry.image.N.repository`, `.tag`, `.digest` | written | Image N, from 0, in body order |
| `file.size` | written | Tar size in bytes (default mode) |
| `registry.tar.count` | written, Tar Per Image `true` | Number of tars (JSON lines in the content) |
| `registry.error` | written on `failure` | Why the webhook was refused |

Every other incoming attribute stays on the FlowFile. With Tar Per Image `true`, each JSON line in the content holds `filename`, `absolute.path`, `X-Sha256`, `file.size`, `registry.host`, `registry.repository`, `registry.tag`, `registry.digest` and `registry.image.count` with `registry.image.0.*`.

## PullContainerImages

Reads no attributes. Writes the same set as Export's default mode except `registry.image.*`, `registry.tar.count` and `registry.error`, plus one attribute per dynamic property (Expression Language over the attributes above).

## PushContainerImages

| Attribute | Direction | Value |
|---|---|---|
| Any attribute named in an Expression Language property | read | Target Registry URL, Input Tar Path and Rewrite Target Base Path evaluate FlowFile attributes, for example `${filename}` |
| `registry.host` | written | Host of Target Registry URL |
| `registry.images` | written | One `repository:tag@digest` per image |
| `registry.pushed`, `registry.present` | written | Tags pushed, and tags already at their digest |
| `registry.tar.leftover` | written | The path of a tar that was pushed but could not be deleted |
| `registry.error` | written on `failure` | Why the push failed |
