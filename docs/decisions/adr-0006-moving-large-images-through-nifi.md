---
title: Moving large images through NiFi
type: decision
status: accepted
tags: [nifi, python, streaming, diode, ExecuteStreamCommand, ListenHTTP]
updated: 2026-10-14
---

# ADR 0006: Moving large images through NiFi

Evidence levels: PROVEN means run and measured, SOURCE means read in the NiFi source, UNVERIFIED means neither. Tests ran on NiFi 2.11 and 2.12 in a lab and on a 3-node cluster with a 1.5 GB heap. The owner validates the chosen option at work before any release.

## Context

The NiFi 2.x Python API cannot stream. A processor sees FlowFile content only as one `bytes` object (`getContentsAsBytes`) or a text reader, and returns content only as one `bytes` object.

- SOURCE: `StandardInputFlowFile.getContentsAsBytes` (apache/nifi `rel/nifi-2.12.0`, `nifi-py4j-bridge`) allocates `new byte[size]` and throws `IOException` above `Integer.MAX_VALUE`. The input and output classes are byte-identical in the nifiapi Python files from 2.0.0 to 2.12.0, and on main.
- PROVEN, 3 GiB FlowFile: `Cannot read FlowFile contents into a byte array because size is 3221225472, which exceeds the maximum allowable size of 2147483647 bytes`.
- PROVEN: Python and Java together need about 13 times the content size in RAM. 128 MB worked on a 1.5 GiB heap (python peak RSS 1.64 GB), 256 MB failed ("Answer from Java side is empty"), and 1 GB on a 1 GiB heap gave `OutOfMemoryError`. The practical cap is far below 2 GiB.
- SOURCE: `ExecuteScript` offers Clojure and Groovy only (Jython was removed in 2.0.0-M1), so a streaming script is not Python.

A container image layer is often several GB. Every option below keeps the layer bytes out of Python memory, in one of three ways: a file on disk, a native NiFi processor (`InvokeHTTP`), or a child process that reads stdin (`ExecuteStreamCommand`).

## Options

| ID | Name | Disk copies in the content repository | Extra disk | Processors | Evidence |
|---|---|---|---|---|---|
| O1 | Staging dir + FetchFile | 1 | staging dir, 1 tar | the existing three | PROVEN, 3 GiB, single node, and the lab flow |
| O2 | One FlowFile per layer | 1 per hop | none | about 16 to 31 | PROVEN, 1 GiB, 3-node cluster |
| O3 | Loopback ListenHTTP | 1 | none | about 6 plus an mTLS service | PROVEN, 1 GiB, 3-node cluster |
| O4 | ExecuteStreamCommand push from stdin | 1 | none | 4 plus a script | PROVEN, 3 GiB lab, 5 MB cluster |

### O1: staging dir + FetchFile (the existing processors)

- **Flow:** Pull or Export (a Python FlowFileSource) writes the tar to a directory, then FetchFile (File to Fetch `${absolute.path}/${filename}`, Completion Strategy `Delete File`) moves it into native FlowFile content, optionally a load-balanced connection, then PutFile and PushContainerImages (Input Tar Path, Delete Input Tar). See [nifi-by-hand.md](../runbooks/nifi-by-hand.md).
- **Pros:** keeps the typed property UI, sensitive properties and Pull's cluster state. The result is native FlowFile content (a FETCH provenance event). The sha256 is equal end to end. No new code.
- **Cons:** staging dir plus content repository is 2 copies of the tar, 3 with PutFile (see [ADR 0002](adr-0002-tar-storage.md)). The writer and FetchFile must stay on one node, so no load balancing between them.
- **Pick when:** the image fits a tar flow, a staging volume exists, and simplicity matters.
- **Failure and recovery:** FetchFile commits first and deletes second, and a failed delete is only logged (SOURCE, `FetchFile.java` lines 316 to 345), so a leftover tar can reappear as a duplicate. Pull's `LEFTOVER_AFTER` handling covers that, and Push skips tags already present. A kill leaves `.<name>.partial` or `<name>.pushing-<id>`; Push adopts a single `.pushing-*` on replay.
- **Evidence:** PROVEN at 3 GiB, single node: python VmHWM 25 MB, Java RSS 1218 MB, sha256 equal, staging 3073 MB to 0 after the fetch. The lab flow with the real Pull, FetchFile, PutFile and Push ran at 54.65 MB. Cluster load balancing after the fetch is UNVERIFIED.

### O2: one FlowFile per layer

- **Flow:** the low side resolves the image: `InvokeHTTP` GET of the manifest (Accept list of OCI and Docker types), a digest check, `SplitJson` over the layers, then `InvokeHTTP` GET of each blob. Every blob is its own FlowFile carrying image name, platform, layer N of M, a check code and its digest. The high side routes blobs and manifests: `InvokeHTTP` HEAD (skip if present), `InvokeHTTP` POST `.../blobs/uploads/?digest=` (monolithic), and a manifest gate script that waits until every blob is present before it PUTs the manifest bytes unchanged. See [nifi-by-hand-o2-layer-flow.md](../archive/nifi-by-hand-o2-layer-flow.md).
- **Pros:** layer bytes never reach Python, so memory is flat. One copy per hop. Manifest digests and tags stay byte-identical, including an OCI index with its children. A layer that is already in the target is dropped without upload. Lost layers are resent by number.
- **Cons:** many processors (about 16 for a single image path, 31 with index handling and recovery). The registry token sits in a FlowFile attribute for its lifetime (1 h), and so in provenance, until an UpdateAttribute deletes it. A failed GET (4xx or 5xx) must be wired to a retry or hold queue, never auto-terminated. The diode must carry many files.
- **Pick when:** digests must be exact, layer-level recovery is needed, and the transfer path already carries many FlowFiles. The diode high side uses this transfer.
- **Failure and recovery:** the gate polls HEAD for each referenced blob (25 s window in the test), then exits non-zero. A LogMessage turns that into a bulletin such as `incomplete: missing layers 2 and 3 of 4. Check code 677a46bf`, and a ControlRate loop re-checks every 30 s. The operator sends `{"image":...,"platform":...,"layers":[2,3],"check":"677a46bf"}` to a low-side ListenHTTP. The flow re-resolves the tag, refuses when the check code differs ("the tag has moved; nothing was sent"), and otherwise sends only those layers plus the config. The manifest is never pushed until every blob is present, and layers already uploaded stay in the target.
- **Evidence:** PROVEN on a 3-node cluster with a 1,048,895,048-byte layer. Round Robin hop: GET on one node, POST on another, 201. Heap peaked at 1.24 GB of 1.5 GB with no growth proportional to the blob. Digests byte-identical (`skopeo inspect --raw` plus `cmp`) for a single-arch Docker schema2 manifest with non-canonical JSON, and for an OCI index and both children. Recovery and the wrong-check refusal PROVEN. `manifest_gate.py` against Quay (lead): a complete manifest exited 0 and `skopeo inspect --raw` was byte-identical; two absent layers gave exit 5 with `missing layers 2 and 3 of 4`, and the check code equals the first 8 hex characters of the stdin sha256. Provenance replay and a Docker manifest list are UNVERIFIED.

### O3: loopback ListenHTTP, no staging disk

- **Flow:** `GenerateFlowFile` (primary node) -> `ExecuteStreamCommand` running a stdlib script that reads the blob from the registry and streams it as a chunked POST to `https://127.0.0.1:<port>/blobs` on the same node -> `ListenHTTP` (Base Path `blobs`, SSL Context Service, Client Authentication `REQUIRED`, Authorized Subject DN, HTTP Headers for Attributes `X-.*|filename`) -> native FlowFile content. See [nifi-by-hand-o3-loopback.md](../archive/nifi-by-hand-o3-loopback.md).
- **Pros:** no staging disk, one copy in the content repository, no Python memory growth (VmHWM 25 MB). Attributes travel as headers.
- **Cons:** `ListenHTTP` has no bind-address property, so it listens on every interface and another pod can reach it. Use mTLS with Authorized Subject DN. A shared token in a header would land in provenance. Jetty rejects an IP host, so the client must send the certificate name as SNI and `Host` while it connects to 127.0.0.1 (otherwise `400 Invalid SNI`). Back pressure is checked only on every 5th request (SOURCE, `ListenHTTPServlet`, `FILES_BEFORE_CHECKING_DESTINATION_SPACE = 5`), then the server answers 503 before reading the body, so the client sees a closed connection and up to 5 FlowFiles can overshoot the threshold. The blob FlowFile and the marker FlowFile are separate and correlate only through headers.
- **Pick when:** no staging disk is allowed and the extra components are acceptable.
- **Failure and recovery:** a stop mid-upload gives a ListenHTTP bulletin (`EofException`) and no partial FlowFile. The client retries with backoff (4th attempt succeeded after the consumer started); each retry re-reads the blob from the registry.
- **Evidence:** PROVEN at 1 GiB on the 3-node cluster: 5.1 s, VmHWM 25 MB, one copy (+1001 MB), landed size exact and sha256 equal to the registry digest. Lockdown PROVEN: no certificate refused at the handshake, a certificate with the wrong DN got 403, the right one got 200. An earlier single-node test at 3 GiB (no mTLS) ran in 2.4 s with VmHWM 29 MB. `post_blob_loopback.py` over mTLS on the cluster: 5,244,741-byte Quay blob delivered on attempt 1 in 0.1 s, size and sha256 equal to the registry digest, headers landed as attributes. A load-balanced layout is UNVERIFIED.

### O4: ExecuteStreamCommand push from content via stdin

- **Flow:** the blob or manifest FlowFile is already native content. `RouteOnAttribute` on `kind`, `UpdateAttribute` for the reference, then `ExecuteStreamCommand` runs [push_from_stdin.py](../../python/scripts/push_from_stdin.py), which reads stdin and talks to the registry. A `RouteOnAttribute` on `execution.status` drops the pushed FlowFile and a `RetryFlowFile` retries the rest. See [nifi-by-hand-o4-push.md](../archive/nifi-by-hand-o4-push.md).
- **Pros:** no staging disk and no Python memory growth (one 1 MiB buffer). Exit codes route failures. The script is plain stdlib and is edited without a NiFi restart.
- **Cons:** the script must exist at the same path on every node, for example on a shared ReadWriteMany mount. Properties become arguments and environment variables, not the typed property UI. The registry password reaches the script as an environment variable: masked in NiFi, but readable in `/proc/<pid>/environ` and `ps eww` by the same uid and by root. A non-zero exit leaves the partial output on the FlowFile until terminated, and with Output Destination Attribute the only relationship is `original`, so failure shows only in `execution.status`.
- **Pick when:** the layers already exist as FlowFiles (O2, O3) or the tar was unpacked, and the push should not use a Python processor.
- **Failure and recovery:** a kill mid-push leaves the FlowFile intact (exit 137) and a dangling upload in the registry, which the next push replaces. A manifest that arrives before its blobs gets HTTP 400 from the registry (exit 14), and the penalised retry pushes it later, so tune Maximum Retries for large blobs. Content is never auto-terminated: retries_exceeded and failure go to a hold queue.
- **Evidence:** PROVEN with plain ExecuteStreamCommand (not the HandleHttp relay variant). Lab, 3 GiB: Python VmHWM 22 MB, Java RSS +22 MB, the content repository held one copy (+3072 MB), the push took 5.6 s on loopback, and the registry digest equals the source sha256. A kill mid-push gave exit 137, the FlowFile was kept intact, and the registry kept a dangling upload. 3-node cluster, 5 MB: success dropped the FlowFile; a wrong digest (exit 13) and a bad password (exit 10) were each kept and pushed after the fix; a manifest that arrived before its blobs got 400 and the retry healed it; digests were byte-identical for a single-arch image and an OCI index. The generic `push_from_stdin.py` was proven against Quay by the lead: blob exit 0, bad password exit 10, manifest PUT byte-identical (`skopeo inspect --raw`). 1 GiB on the cluster was not run (disk guard). 1 GiB on the cluster was not run (disk guard).

### Rejected

- **MergeContent TAR:** works and the tar is accepted by `read_export`, but it holds 3 live copies in the content repository (blob, tar, unpacked entry), measured 780 MB to 9996 MB for a 3 GiB blob, and needs about 23 processors. One failed fragment voids the whole bin after Max Bin Age.
- **A Python chunk loop:** a FlowFileSource forbids input and a FlowFileTransform needs it (SOURCE, proxy classes), so the loop would be a self-looping Transform, and the registry (Quay tested) ignores `Range` on a blob GET and returns the full body, so every chunk would re-read the blob. Chunk output memory is also about 2.4 times the chunk. The PATCH push half works but does not rescue the export half.
- **Groovy ExecuteScript:** `session.write` streams 3 GB with 564 MB heap, but the script is Groovy, not Python.

## Decision

- **O1 is the default** for tar-shaped flows (Pull and Export as shipped). It needs no new code.
- **The diode high side uses the O2 transfer with the O4 push.** It keeps digests exact, resends lost layers by number, and keeps layer bytes out of Python.
- **O3 is for when no staging disk is allowed.**
- The MergeContent tar, the chunk loop and Groovy are rejected for the reasons above.

No option dominates: the choice is tar-shaped (O1) against layer-shaped (O2, O3, O4). The owner validates the chosen option at work before any release.

## Consequences

- Three stdlib scripts ship in `python/scripts/`, with unit tests against a fake registry or a local HTTP server: `push_from_stdin.py` (O4), `manifest_gate.py` (O2 gate) and `post_blob_loopback.py` (O3 poster). The gate and the poster import `push_from_stdin.py` for authentication, so deploy the three together.
- The by-hand pages are [O4 push](../archive/nifi-by-hand-o4-push.md), [O2 layer flow](../archive/nifi-by-hand-o2-layer-flow.md) and [O3 loopback](../archive/nifi-by-hand-o3-loopback.md).
- A registry token held in a FlowFile attribute (O2) shows in provenance; delete the attribute before the crossing and mint a new token on the high side.
- Open: O4 at 1 GiB on the cluster, Round Robin pairing for O3, provenance replay, and the mirror.py CLI importer reading a MergeContent tar.
