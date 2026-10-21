---
title: Testing checklist
type: runbook
status: draft
updated: 2026-10-14
---

# Testing checklist

Use it when testing at work, once per storage option from [ADR 0002](../decisions/adr-0002-tar-storage.md). Every expected result is Unverified until tested: record what you saw next to it. Hosts below are placeholders: `registry.example.com` (source), `registry-high.example.com` (target), `nifi.example.com`.

Build the flow first with [nifi-by-hand.md](nifi-by-hand.md) and send a webhook with QUICKSTART section 5.

## 1. The image arrived intact

- **Where:** a shell with skopeo.
- **Do:** `skopeo inspect --raw docker://registry-high.example.com/team/app:1.0 | sha256sum`
- **Expect:** the digest equals the one in the webhook body. Quay (or the target registry's UI) shows the same digest on the tag.

## 2. Provenance and attributes

- **Where:** right-click PushContainerImages > **View data provenance**; open the newest event; **Attributes** tab.
- **Expect:** `registry.pushed` or `registry.present` is at least 1; `registry.images` lists `repository:tag@digest`. On the Export event: `registry.image.count` and `registry.image.N.*` match the body, and any `X-CI-*` headers sent are still present.
- **Lineage:** open the event, **Lineage** icon: ListenHTTP, RouteOnAttribute, ExportContainerImages, PushContainerImages.

## 3. The tar is removed after Push

- **Where:** a shell on the node that ran Push.
- **Do:** `ls -la /data/out` (the value of `gitlab.container.tar.dir`).
- **Expect (measured, lab NiFi 2.11, 9.66 GB image):** with Delete Input Tar `true`, no `container-images-*.tar` and no `*.pushing-*` file after a successful push. After a failed push the tar stays. Disk use was 1x the tar: Push renames in place, then deletes.

## 4. A bad webhook exports nothing

- **Do:** post the body with one digit of a digest changed, then a body whose `X-Sha256` is wrong, then a repository outside Allowed Repositories.
- **Where:** the `failure` queue of Export (right-click the connection > **List queue** > a row > **Attributes** > `registry.error`).
- **Expect:** `registry.error` names the cause, and `ls /data/out` shows no tar.

## 5. Disk use against the 50% line (content repository PVC option)

- **Where:** `df -h <mount>` on each node, while Export runs the largest expected webhook.
- **Expect:** peak usage, content plus tars, stays under `nifi.content.repository.archive.max.usage.percentage` (default 50%). Check the value in `conf/nifi.properties`. Measured: above `nifi.content.repository.archive.backpressure.percentage` (max.usage + 2% when unset) content writes block until the tars are deleted, archive cleanup cannot free them, and at 100% writes fail with "No space left on device".
- **Also:** send two big webhooks in a row with Export at 1 concurrent task, and watch the peak with `watch -n 5 df -h <mount>`.

## 6. Same-node rule

- **Do:** on a cluster, set the Export to Push connection to **Load Balance Strategy** `Do not load balance` (right-click the connection > **Configure** > **Settings**), send 10 webhooks.
- **Expect:** every push finds its tar. Then set the strategy to `Round robin` with node-local volumes.
- **Expect (to confirm):** some pushes fail with `Input Tar Path ... does not exist` in `registry.error`. With shared storage at the same path, none fail.

## 7. A node restarts mid-export

- **Do:** send a large webhook, restart the NiFi node while Export runs (`kill` the NiFi process or restart the pod).
- **Where:** after restart, `ls -la /data/out` and the Export processor's bulletins.
- **Expect:** measured: after a kill Export leaves `.<name>.partial` and Push leaves `<name>.pushing-<id>`; neither is picked up as a finished tar. Push now adopts a single `<path>.pushing-*` when the replayed FlowFile finds Input Tar Path missing, and fails naming all of them when there are several. Still to confirm: the FlowFile is re-queued and Export runs again. A stale `.partial` is not matched by the cleanup flow's default filter, which skips hidden files.

## 8. Parameter change

- **Do:** in **Parameter Contexts**, change `gitlab.container.tar.dir` to a second directory, apply.
- **Expect (to confirm):** NiFi stops and restarts the processors that use it without a NiFi restart, and the next export lands in the new directory.

## 9. Tar Per Image

- **Do:** set Tar Per Image `true`, add SplitText and EvaluateJsonPath, post a webhook with two images.
- **Expect:** two tars in `/data/out`, two FlowFiles reach Push, each with its own `registry.repository`, `registry.tag` and `registry.digest`. Compare the bytes written (`du -sh /data/out` before Push deletes them) with the default mode to see the repeated layers. Measured with 2 images sharing 4 of 5 layers: 1.92 times the bytes of one tar.

## 10. Cleanup flow

- **Do:** leave a tar from a failed push for more than the Minimum File Age (use 1 minute for the test), with ListFile > FetchFile (Completion Strategy `Delete File`) > auto-terminate.
- **Expect (to confirm):** the tar is deleted, and a younger tar is left alone.
