---
title: Testing checklist
type: runbook
status: draft
updated: 2026-10-22
---

# Testing checklist

Use it when testing the image transfer at work. Every expected result is Unverified until tested: record what you saw next to it. Hosts below are placeholders: `registry.example.com` (source), `registry-high.example.com` (target), `nifi.example.com`.

Build the flow first with [the image transfer build guide](image-transfer-build-by-hand.md), or on one NiFi with [QUICKSTART.md](../../QUICKSTART.md), and send one request as in the guide's L9.

## 1. The image arrived intact

- **Where:** a shell with skopeo.
- **Do:** `skopeo inspect --raw docker://registry-high.example.com/team/app:1.0 | sha256sum`
- **Expect:** the digest equals the one in the request body. The target registry's UI shows the same digest on the tag, and every config and layer digest exists with its planned size.

## 2. Provenance and attributes

- **Where:** right-click the high-side RouteRegistryTransfer > **View data provenance**. Open the newest event, **Attributes** tab.
- **Expect:** `registry.kind`, `registry.repository`, `registry.digest` and `registry.size` match the plan. `registry.auth.header` is empty. `registry.error` is empty after a clean pass.
- **Lineage (one NiFi only, lineage does not cross a diode):** open a blob event, **Lineage** icon: ListenHTTP, PlanRegistryTransfer, SplitJson, RouteRegistryTransfer, InvokeHTTP, CryptographicHashContent on the low side, then RouteRegistryTransfer and InvokeHTTP on the high side.

## 3. Queues drain

- **Where:** the canvas of both groups.
- **Expect:** every work queue drains to zero. The queues into the Hold cards (disabled LogMessage) stay empty.

## 4. A bad request plans nothing

- **Do:** post the body with one digit of a digest changed, then a body whose `X-Sha256` is wrong, then a repository outside Allowed Repositories.
- **Where:** the queue from PlanRegistryTransfer `failure` into Hold (right-click the connection > **List queue** > a row > **Attributes** > `registry.error`).
- **Expect:** `registry.error` names the cause, as listed in the [input contract](../reference/contract.md). SplitJson receives nothing.

## 5. Memory stays flat

- **Where:** the NiFi node's Python process (`ps -o rss` or the pod's memory graph) while the largest expected image moves.
- **Expect:** Python memory does not grow with the blob size. RouteRegistryTransfer never reads blob content.

## 6. A manifest waits for its layers

- **Do:** on a dedicated test image only, make one layer missing on the high side as the [recovery proof](image-transfer-recovery-proof.md) did, then send the image again by tag.
- **Expect:** the high-side manifest goes to `waiting` and sits in the RouteTransfer to WaitForLayers queue. List that queue: `registry.wait.received` of `registry.wait.expected` and `registry.wait.missing` name what is still awaited, `registry.error` names the layer numbers and the check code. A layer that arrives late releases the manifest at once through SignalOwner, and it is PUT by digest, then by tag. With a layer still missing after RetryWait's Maximum Retries, ReportIncomplete logs a WARN bulletin and the manifest waits in the IncompleteImages queue.

## 7. Resend chosen layers

- **Do:** follow the build guide's L11 with `registry.layers` and the `registry.check` code from the bulletin.
- **Expect:** only the config and those layers are planned, and the waiting manifest completes. A wrong check code fails the FlowFile with `the tag has moved; nothing was sent`.

## 8. A large image

- **Do:** move an image with a multi-GB layer. This is still open: the evidence so far is small images and one 1 GiB layer.
- **Expect (to confirm):** the content repository holds the blob as one claim per hop, queue size thresholds above the blob let it pass, and the target digest matches.
