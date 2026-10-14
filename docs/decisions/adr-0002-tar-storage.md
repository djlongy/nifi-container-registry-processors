---
title: Where the export tar lives
type: decision
status: proposed
updated: 2026-10-14
---

# ADR 0002: Where the export tar lives

Behaviour claims about options A to C are Unverified until tested. Option D and the one-copy disk rule were measured on a lab NiFi 2.11 with a 9.66 GB image. Statements marked "NiFi docs" come from the NiFi 2.x Administration Guide and Python Developer Guide.

## Context

Export (or Pull) writes a tar that can be many GB. Push (or FetchFile) reads it. The Python API passes FlowFile content as one in-memory `bytes` object (`getContentsAsBytes()`, `FlowFileTransformResult.contents`), with no streaming (NiFi source, `nifiapi/flowfiletransform.py`).

## Options

### A. FlowFile content

- Pros: no disk path to manage, works across nodes by itself, and shows in provenance.
- Cons: the whole tar is held in memory on both sides, so a multi-GB image does not fit. There is no streaming API.
- Pick when: never for real images. Small test tars only (Push accepts an empty Input Tar Path for this).

### B. Dedicated per-node volume, same path on every node (recommended)

- Pros: tars never compete with NiFi for space. Push reads local disk, which is fast. Needs no shared filesystem.
- Cons: the Export to Push connection must stay on `Do not load balance`, or Push may run on a node without the file (Unverified until tested). A node loss loses its tars (Pull regenerates them, Export needs a new webhook).
- Pick when: you can attach a volume to each node.

### C. Shared storage (ReadWriteMany, NFS)

- Pros: any node can read any tar, so load balancing the Export to Push connection is safe (Unverified until tested).
- Cons: a shared filesystem to run, slower than local disk, and file locking and rename behaviour depend on the NFS version (Unverified until tested).
- Pick when: the connection between Export and Push is load-balanced, or tars must survive a node loss.

### D. A folder on the content repository PVC

- Pros: needs no new volume.
- Cons: tars share free space with NiFi. NiFi docs: archive cleanup starts at `nifi.content.repository.archive.max.usage.percentage` (default 50%), and content writes are refused at `nifi.content.repository.archive.backpressure.percentage` (default 2 points higher, 52%) until usage falls below the first. Tars are not archive, so archive cleanup cannot free them. Above the backpressure percentage content writes block until the tars are deleted, and at 100% they fail with "No space left on device" (measured). The folder must also stay outside NiFi's repository directories: NiFi leaves it alone while running, but on restart the FileSystemRepository startup scan deletes every file it did not write (archive disabled) or tries to archive it (archive enabled) (measured). Changing `nifi.content.repository.directory.default` does not move existing content and queued FlowFiles then fail with ContentNotFoundException, so copy the old directory with `cp -a` before starting NiFi (measured).
- Pick when: it is the only volume. Follow the sizing rules in the README ("If your only volume is the content repository PVC").

## Decision

Option B is recommended. Option C only with a load-balanced connection. Option D with the sizing rules. Option A is not viable.

## Consequences

- The Tar Output Directory and Input Tar Path tooltips state the same-node rule.
- The directory is set from a parameter ([ADR 0003](adr-0003-directory-by-parameter.md)).
- Tars of failed pushes stay on disk, and a crash or kill leaves `.<name>.partial` (Export) or `<name>.pushing-<id>` (Push) (measured). The README gives a ListFile, FetchFile cleanup flow.
- Export then Push on one directory needs 1x the largest export, not 2x: Push renames the tar in place and Delete Input Tar removes it (measured). Two copies exist only where one is made, such as PutFile onto a second volume or the staging directory plus FetchFile hybrid.
