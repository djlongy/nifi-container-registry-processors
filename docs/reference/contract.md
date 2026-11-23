---
title: Input contract
type: reference
status: current
updated: 2026-10-22
---

# Input contract

What each Python processor that accepts input requires from the FlowFile it receives, and what happens when the input is missing or wrong. Every fact comes from the processor code in `python/container-registry/`. For the attributes a processor writes, see [attributes.md](attributes.md). Property tables are in the [README](../../README.md).

Contents:

- [Standing in for the webhook with GenerateFlowFile](#standing-in-for-the-webhook-with-generateflowfile)
- [PlanRegistryTransfer](#planregistrytransfer)
- [RouteRegistryTransfer](#routeregistrytransfer)

Conventions: a processor that refuses its input sends the FlowFile to the relationship named in the table and sets the attribute `registry.error` to the text shown. `{name}` marks a value the processor fills in. A **UI display name** is written as the NiFi UI shows it.

## Standing in for the webhook with GenerateFlowFile

A GenerateFlowFile starts with **none of the webhook's attributes**. The real webhook comes from gitlab-registry-mirror (`mirror.py`, function `notify()`), which sends the request body as the HTTP POST body and every attribute as an HTTP header. ListenHTTP turns each header whose name matches its **HTTP Headers for Attributes** regular expression into a FlowFile attribute with the same name, so a FlowFile from the real webhook carries the attributes below without anyone adding them. A GenerateFlowFile carries only `mime.type` (from **Mime Type**), `filename`, `path`, `uuid` and the dynamic properties you add, so every attribute a downstream processor or RouteOnAttribute rule needs must be added by hand, one dynamic property each.

If a RouteOnAttribute rule tests an attribute that the GenerateFlowFile does not have, the FlowFile goes to `unmatched` and is not routed. If PlanRegistryTransfer in webhook mode does not find a correct `X-Sha256`, it fails the FlowFile with `X-Sha256 is missing or is not the sha256 of the body`.

### What a GenerateFlowFile must carry, by what it feeds

| Feeds | Content (Custom Text) | Dynamic properties to add |
|---|---|---|
| PlanRegistryTransfer in reference mode (for example the GenerateFlowFile card that the runbook suggests naming `ResendImage`) | Ignored. `{}` | `registry.image.reference`, which is the only attribute PlanRegistryTransfer needs, **plus the full `X-*` set** when anything downstream routes on it (see below). Add `transfer.id`, `registry.layers`, `registry.check` and `registry.platform` when you need them, see [PlanRegistryTransfer](#planregistrytransfer) |
| PlanRegistryTransfer in webhook mode, directly (the build guide's ListenHTTP) | The request body, byte for byte | `X-Sha256` |
| The same, behind a RouteOnAttribute on `X-Artifact-Type`, if your flow adds one | The request body, byte for byte | `X-Sha256` and `X-Artifact-Type` |

PlanRegistryTransfer in reference mode reads none of the webhook headers itself. **A stand-in card must still carry the full `X-*` set of the real webhook whenever anything downstream routes on them**: a RouteOnAttribute, or a person reading the FlowFile attributes. The GenerateFlowFile card in the image transfer flow (suggested name `ResendImage`) carries all of them: `X-Artifact-Type` = `container-images-request`, `X-Artifact-Format` = `json`, `X-Sha256` (the sha256 of its Custom Text `{}`, `44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a`), `X-Registry` = `registry.example.com`, `X-Image-Count` = `1`, `X-CI-Project` = `manual-resend`, `X-CI-Pipeline-Url` = `none` and `X-CI-Commit-Sha` = `none`, next to `registry.image.reference`. See [the table below](#the-complete-webhook-attribute-set) for what each one is.

### The complete webhook attribute set

These are all the attributes a real webhook FlowFile carries, with the exact names and values `notify()` sends. Add each one you need as a dynamic property (**Properties, +**) on the GenerateFlowFile. Names are case-sensitive.

| Dynamic property (attribute) | Required for | Example value | What reads it |
|---|---|---|---|
| `X-Sha256` | PlanRegistryTransfer in webhook mode | `3f2a...` (64 lowercase hex characters, see below) | PlanRegistryTransfer compares it with the sha256 of the FlowFile content |
| `X-Artifact-Type` | A RouteOnAttribute that routes on it | `container-images-request` | The RouteOnAttribute rule `${X-Artifact-Type:equals('container-images-request')}`. No Python processor reads it |
| `X-Artifact-Format` | Nothing, unless a route tests it | `json` | Nothing. Routing and display only |
| `Content-Type` | Nothing | `application/json` | Nothing. Routing and display only. A GenerateFlowFile **Mime Type** sets `mime.type`, which is a different attribute |
| `X-Registry` | Nothing, unless a route tests it | `registry.example.com` | Nothing. The registry the processor trusts is the body's `registry` field and **Source Registry URL**, never this header |
| `X-Image-Count` | Nothing, unless a route tests it | `1` | Nothing. Routing and display only |
| `X-CI-Project`, `X-CI-Pipeline-Url`, `X-CI-Commit-Sha` | Nothing, unless a route tests them | `team/app`, `https://ci.example/1`, `0a1b2c3` | Nothing. Set only inside GitLab CI, routing and display only |

### Compute `X-Sha256` for a hand-written body

`X-Sha256` must be the sha256, in lowercase hexadecimal, of the **exact bytes** of the FlowFile content. PlanRegistryTransfer compares it with `hashlib.sha256(content).hexdigest()`, so one changed character, a trailing newline, a different line ending or an upper-case hex digit fails the check.

Write the body into a shell variable with `printf '%s'` (which adds no newline) and hash that:

```sh
BODY='{"registry": "registry.example.com", "images": [{"repository": "team/app", "tag": "1.0", "digest": "sha256:<64 hex>"}]}'
printf '%s' "$BODY" | sha256sum | cut -d' ' -f1      # macOS: shasum -a 256
```

Rules for **Custom Text**:

1. Paste the same bytes you hashed, with no trailing newline or space and no line wrapping added by the editor.
2. Leave **Data Format** `Text`, **Unique FlowFiles** `false` and **Character Set** `UTF-8`. With those settings GenerateFlowFile writes Custom Text as the content. **File Size** is then ignored.
3. Custom Text is evaluated as Expression Language. A body holds no `${`, so it is written unchanged. Do not put `${` into a body.
4. Hash the final text. If you change one character of Custom Text, compute `X-Sha256` again.

`mirror.py` writes the body with Python's `json.dumps` (a `", "` and `": "` separator) and adds a `references` field (`repository:tag` lines). PlanRegistryTransfer reads only `registry` and `images`, so a hand-written body needs only those two fields. Any byte layout works as long as `X-Sha256` matches it.

To check, view the FlowFile content in NiFi, download it and run `sha256sum` on it: the result must equal the `X-Sha256` attribute.

## PlanRegistryTransfer

A FlowFileTransform: input is required. It reads either the attribute `registry.image.reference` (reference mode) or, when that attribute is absent or empty, the content as a webhook body (webhook mode). Relationships: `success` and `failure`. Every refusal is `failure` with `registry.error`, and no job is planned.

### Attributes

| Attribute | Required | Format | Normally set by | If missing or wrong |
|---|---|---|---|---|
| `registry.image.reference` | Required in reference mode | `repository:tag`, `repository@sha256:<64 hex>` or `repository:tag@sha256:<64 hex>`. Relative to **Source Registry URL**: no host. A tag or a digest must be present | A GenerateFlowFile such as `ResendImage` | `failure`, `not a repository:tag or repository@sha256:digest reference: '{reference}'`. A repository outside **Allowed Repositories**: `{repository} is not in Allowed Repositories` |
| `X-Sha256` | Required in webhook mode | 64 lowercase hex characters, the sha256 of the content bytes | The sender, through ListenHTTP | `failure`, `X-Sha256 is missing or is not the sha256 of the body`. Not read in reference mode |
| `transfer.id` | Optional | Any text. Copied to every job and to the output | The caller | A UUID is generated |
| `registry.layers` | Optional | Layer numbers from 1, separated by commas or spaces, for example `2,3`. Selects a layers-only resend of one image: the config and those layers, no manifest | A GenerateFlowFile used for recovery | `failure`: `registry.layers must be layer numbers such as 2,3: '{text}'` for a non-number; `registry.layers needs numbers from 1 and the registry.check code of the incomplete bulletin` for a number below 1 or no `registry.check`; `registry.layers resends one image: the request names {n}` for several images; `{label} has no layer {n}: it has {m}` for a number the image does not have |
| `registry.check` | Required with `registry.layers` | The 8 hex characters after `sha256:` of the top manifest, as the incomplete bulletin prints them | The operator, from the bulletin | `failure`, `refused: check code {given} does not match {current} for {label}, the tag has moved; nothing was sent` |
| `registry.platform` | Optional, with `registry.layers` | `os/architecture[/variant]`, for example `linux/amd64`. Replaces **Platform Selection** for this resend | The operator, from the bulletin | A platform the image does not have: `no platform of {repository}:{tag} matches Platform Selection` |

### Body (webhook mode)

The body only selects images. Every field is checked, after `X-Sha256`, before the registry is read. A JSON object (UTF-8):

```json
{"registry": "registry.example.com",
 "images": [{"repository": "team/app", "tag": "1.0", "digest": "sha256:<64 hex>"}]}
```

| Field | Type | Required | Format and rule | If missing or wrong |
|---|---|---|---|---|
| `registry` | string | Required | The host of **Source Registry URL**, compared in lower case with a trailing `:443` removed | `failure`, `registry.error` = `the body names registry {registry}, not {host}` |
| `images` | array | Required | One or more objects. Duplicates are merged, order is kept | `failure`, `the body is not JSON {"registry": "host", "images": [{"repository", "tag", "digest"}]}` |
| `images[].repository` | string | Required | Must be in **Allowed Repositories** (an exact name, or a prefix ending in `*`). Lower case path, for example `team/app` | `failure`, `{repository} is not in Allowed Repositories`, or for a malformed name `not a valid repository, tag and digest: {repository}:{tag}@{digest}` |
| `images[].tag` | string | Required | `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}` | `failure`, `not a valid repository, tag and digest: {repository}:{tag}@{digest}` |
| `images[].digest` | string | Required | `sha256:` followed by 64 lowercase hex characters | `failure`, `not a valid repository, tag and digest: {repository}:{tag}@{digest}` |
| `references` | string | Optional | `repository:tag` lines. Ignored | None |

Any other shape (not JSON, not UTF-8, an empty `images` array, a value that is not a string) gives the `the body is not JSON ...` error. Other fields are ignored.

A digest-pinned request fails when the tag is elsewhere.

### Checks against the registry

| Condition | Result |
|---|---|
| A tag is missing, or is not at the digest the request pinned | `failure`, `{repository}:{tag} is not at {digest or 'any digest'} in {host}` (with the digest text `any digest` when none was pinned) |
| No platform matches **Platform Selection** | `failure`, `no platform of {repository}:{tag} matches Platform Selection` |
| An image plans more jobs than **Maximum Jobs Per Transfer** | `failure`, `{repository}:{tag} needs more than Maximum Jobs Per Transfer ({n})` |
| A layer is missing in the source registry | `failure`, `blob {digest} is missing` |
| Indexes nest deeper than **Maximum Manifest Depth** | `failure`, `{repository}@{digest}: indexes nested deeper than Maximum Manifest Depth ({n})` |

## RouteRegistryTransfer

A FlowFileTransform: input is required. It reads the **job** attributes, and on the low side's first pass the job JSON in the content. A FlowFile reaches it from SplitJson (low, first pass), from the native InvokeHTTP callbacks and RetryFlowFile (both sides), and from the diode (high, first pass). Relationships: `prepared`, `unverified`, `complete`, `retryable`, `signal`, `waiting`, `failure`. A FlowFile also returns from Notify (after `signal`) and from Wait and RetryWait (after `waiting`).

Most refusals are a `RegistryError`: the processor sends the FlowFile to `retryable` with `registry.retry.max` = `3`, so RetryFlowFile retries it three times and then sends it to `retries_exceeded`. Only the three Python exception types `ValueError`, `KeyError` and `TypeError` (a missing or malformed field) go to `failure` with `invalid registry job: {exception name}`.

### Low side, first pass: the job JSON in the content

Used when the FlowFile has no `registry.kind` attribute. SplitJson produces it. Content larger than 64 MiB is refused with `registry job exceeds 64 MiB` (`retryable`).

| Field | Type | Required | Format | If missing or wrong |
|---|---|---|---|---|
| `kind` | string | Required | `blob`, `config`, `manifest` or `index` | `failure` `invalid registry job: KeyError` if absent; `retryable` `unknown registry job kind` if another value |
| `repository` | string | Required | A repository name | `retryable`, `invalid registry repository or digest` |
| `digest` | string | Required | `sha256:` and 64 lowercase hex characters | `retryable`, `invalid registry repository or digest` |
| `size` | integer or digit string | Required | Digits only | `retryable`, `invalid registry job size` |
| `mediaType` | string | Required | The media type of the object | `failure`, `invalid registry job: KeyError` if absent |
| `reference` | string | Required | The tag, or the digest, a manifest is published under | `failure`, `invalid registry job: KeyError` if absent |
| `image` | string | Required | `repository:tag`, used in bulletins | `failure`, `invalid registry job: KeyError` if absent |
| `platform` | string | Required | `os/architecture[/variant]`, or `-` | `failure`, `invalid registry job: KeyError` if absent |
| `check` | string | Required | 8 hex characters | `failure`, `invalid registry job: KeyError` if absent |
| `needs` | array of strings | Required | The digests the object depends on: a manifest's config and layers, an index's manifests | `failure`, `invalid registry job: KeyError` if absent |
| `layers` | integer | Optional | The layer count. Default `0` | None |
| (content) | JSON | Required | The whole content must be one JSON object | `failure`, `invalid registry job: JSONDecodeError` |
| `body` | string | Required for `manifest` and `index` | Base64 of the exact manifest bytes | `failure`, `invalid registry job: KeyError` if absent, `invalid registry job: Error` if not valid base64 |

The processor turns these fields into the `registry.*` attributes below and, for a manifest or an index, replaces the content with the decoded manifest bytes.

### Job attributes (both sides, every pass after the first)

On the high side these arrive from the low side through the diode unchanged.

| Attribute | Required | Format | Normally set by | If missing or wrong |
|---|---|---|---|---|
| `registry.kind` | Required | `blob`, `config`, `manifest` or `index` | RouteRegistryTransfer (low) | `retryable`, `unknown registry job kind` |
| `registry.repository` | Required | A repository name | RouteRegistryTransfer (low) | `retryable`, `invalid registry repository or digest` |
| `registry.digest` | Required | `sha256:` and 64 lowercase hex characters | RouteRegistryTransfer (low) | `retryable`, `invalid registry repository or digest` |
| `registry.size` | Required | Digits only. The exact content size in bytes | RouteRegistryTransfer (low) | `retryable`, `invalid registry job size`. On the high side a content size that differs: `failure`, `received content size differs from the planned job`. After the low hash, `failure`, `content digest or size differs from the planned job` |
| `registry.media.type` | Required for `manifest` and `index` | A manifest media type such as `application/vnd.oci.image.manifest.v1+json` | RouteRegistryTransfer (low) | `retryable`, `unsupported manifest media type`. For other kinds it is read but not checked |
| `registry.reference` | Required for `manifest` and `index` | The tag, or the digest when the manifest has no tag | RouteRegistryTransfer (low) | A reference that is not a tag and not the digest: `retryable`, `invalid manifest tag` |
| `registry.needs` | Required for `manifest` and `index` | A JSON array of up to 1000 `sha256:` digests | RouteRegistryTransfer (low) | Missing: `failure`, `invalid registry job: TypeError`. Not an array or too long: `retryable`, `invalid manifest dependencies`. An item that is not a digest: `retryable`, `invalid dependency digest` |
| `registry.image` | Optional | `repository:tag`. Used in the message | RouteRegistryTransfer (low) | Shown as `None` in messages |
| `registry.platform` | Optional | `os/architecture[/variant]`, or `-` | RouteRegistryTransfer (low) | Shown as `None` in messages |
| `registry.check` | Optional | 8 hex characters | RouteRegistryTransfer (low) | Shown as `None` in messages |
| `registry.layer.count` | Optional | Digits | RouteRegistryTransfer (low) | Not read |

### Attributes the processor reads on a callback

| Attribute | Set by | Meaning | If missing or wrong |
|---|---|---|---|
| `registry.stage` | RouteRegistryTransfer | `fetch`, `verify`, `upload`, `manifest-digest` or `manifest-tag`. Empty for a new job. Do not set by hand | Empty: the FlowFile is treated as a new job |
| `invokehttp.status.code` | InvokeHTTP | The HTTP status of the native request | Missing counts as `0`. `0`, `401`, `408`, `429` and `5xx`: `retryable`, `native {stage} failed (HTTP {status}); retry with fresh authentication`. Other codes: `failure`, `native {stage} refused (HTTP {status})` |
| `content_SHA-256` | CryptographicHashContent | The digest of the downloaded bytes, without `sha256:` | Missing or different: `failure`, `content digest or size differs from the planned job` |
| `resp.Docker-Content-Digest` (or `resp.docker-content-digest`) | InvokeHTTP, **Response Header Request Attributes Prefix** `resp.` | The digest the destination reports for a manifest PUT | Missing or different: `failure`, `destination manifest digest differs from the planned job` |
| `registry.manifest.tag.url` | RouteRegistryTransfer | The tag PUT that follows the digest PUT | Set by the processor |
| `registry.owners` | PlanRegistryTransfer, carried by the low side | JSON list of the manifest or index digests that need this object | Missing (a 1.6.0 low side): no signal is sent, a waiting manifest moves on at its next Wait expiry. Not a list of digests: `failure`, `invalid registry job: ValueError` |
| `registry.wait.retries` | RetryWait | RetryWait's count across one wait | Set by RetryFlowFile. Blanked by the processor when the manifest leaves the wait |

### Other refusals

| Condition | Result |
|---|---|
| Low: the source blob is missing | `failure`, `source blob is missing` |
| High: **Target Repository** is not a valid repository name | `retryable`, `invalid target repository` |
| High: the registry names an upload location on another host | `retryable`, `upload Location names a foreign host` |
| High: the blob HEAD answers other than 200 or 404 | `retryable`, `destination blob HEAD failed (HTTP {status})` |
| High: a dependency HEAD answers other than 200 or 404 | `retryable`, `manifest dependency HEAD failed (HTTP {status})` |
| High: a manifest whose layers or platform manifests have not all arrived | `waiting`, `{image} ({platform}) incomplete: missing {what}. Check code {check}`, with `registry.wait.received`, `registry.wait.expected`, `registry.wait.missing` and `registry.manifest.digest`. After RetryWait's Maximum Retries: ReportIncomplete logs it and IncompleteImages holds it |
