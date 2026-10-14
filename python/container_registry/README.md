# container_registry

PushContainerImages, PullContainerImages and ExportContainerImages as NiFi Python processors: the maintained implementation of the mirror.py export tar for NiFi, plain source with no build.

## More documentation

[../../docs/README.md](../../docs/README.md) maps the decision records (why Python, where the tar lives, parameters, one tar or many, webhook trust), the click-by-click runbook, the testing checklist and the attribute reference.

## Requirements

- Apache NiFi 2.x with Python enabled (`nifi.python.command`), Python 3.10 or later on the NiFi host.
- Nothing from PyPI. `python/requirements.txt` lists no packages and sits outside this directory, so NiFi never runs `uv` for it and `UV_INDEX_URL` is not consulted.
- Disk on the node for the largest export, once when Export and Push use the same directory: Push renames the tar in place and Delete Input Tar removes it (measured 1x with a 9.66 GB image). Plan for two copies only where a copy is made, such as PutFile onto a second volume, or the staging directory plus FetchFile hybrid, where the content repository holds the second copy.

## Install

Copy this directory into the Python extensions directory (`nifi.python.extensions.source.directory.default`, `./python_extensions`) of every node, or onto the share mounted there:

```sh
cp -r python/container_registry /opt/nifi/nifi-current/python_extensions/
```

On Kubernetes, build `python/Containerfile` and copy from it in an init container, so every pod has the processors before NiFi starts:

```yaml
initContainers:
  - name: python-extensions
    image: <registry>/<ns>/nifi-container-registry-python@sha256:<digest>
    command: ["sh", "-c", "rm -rf /target/container_registry && cp -r /extensions/container_registry /target/"]
    volumeMounts:
      - name: python-extensions
        mountPath: /target
```

NiFi discovers the processors within a minute, listed as `PullContainerImages 1.5.0`, `PushContainerImages 1.5.0` and `ExportContainerImages 1.5.0` (bundle `python-extensions`). An edited file reloads when the processor next starts.

## Properties

Generated from the descriptors (`python3 python/tests/test_readme_tables.py --write`); the unit tests fail when they drift. Pull, Export and Push each have one registry, so Pull and Export say `Source Registry` and Push says `Target Registry`. For the property names of 1.4.x see the migration table in the top-level README.

**PullContainerImages** (dynamic properties become attributes on each FlowFile, Expression Language over the tar's attributes)

<!-- properties:PullContainerImages -->
| Name | Required | Default | Description |
|---|---|---|---|
| Source Registry URL | Yes | none | Registry to read images from, such as https://registry.example.com, or https://host/artifactory/api/docker/repo-key for Artifactory. No user:password in the URL. Required, no default. Environment variables allowed: ${REGISTRY_URL}. |
| Source Registry Username | No | none | User for the source registry. Empty (default): anonymous access, which works only on a registry that allows it. Environment variables allowed: ${REGISTRY_USER}. |
| Source Registry Password | No | none | Password or access token for the source registry. Empty (default): none sent. Sensitive: reference a parameter, such as #{registry_password}. |
| SSL Context Service | No | none | A StandardSSLContextService whose PKCS12 or JKS truststore holds the CA of the registry and the Ledger URL (PEM is not accepted). Added to the system store and SSL_CERT_FILE. Empty (default): only those are trusted. Keystore ignored. |
| Trusted Storage Hosts | No | none | Hosts, IPs or CIDR ranges the registry may redirect blob transfers to although they are private, such as an on-premises MinIO: minio.internal, 10.20.0.0/16. Empty (default): private, loopback, link-local and http-from-https redirects are refused. The registry's own host is always allowed. |
| Image Source | Yes | `Registry Listing` | What a run exports. Default Registry Listing: tags of Repositories to Export whose digest changed since the last export. Ledger: images the mirror ledger says it synced. Image List: the images in Image List. |
| Repositories to Export | No | none | Registry Listing only. Repositories, comma or new line separated: team/app, team/* for a prefix, * for all. Empty (default): nothing is exported. Environment variables allowed. |
| Tag Filter | Yes | `(?!sha256-).+` | Registry Listing only. Regular expression a tag must match in full. Default (?!sha256-).+ skips the sha256-... signature and attestation tags. Use .+ for every tag, v[0-9.]+ for release tags. |
| Ledger URL | No | none | Ledger only. URL of the mirror pipeline's ledger.json, read only, such as https://gitlab.example.com/api/v4/projects/1/repository/files/ledger.json/raw?ref=main. The ledger must name the Source Registry URL host. No default. |
| Ledger Token | No | none | Ledger only. Token sent to the Ledger URL in the header named by Ledger Token Header. Empty (default): no header sent. It is not sent to a redirect target. |
| Ledger Token Header | No | `PRIVATE-TOKEN` | Ledger only. Header that carries Ledger Token: PRIVATE-TOKEN (default, GitLab) or Authorization for a value such as Bearer <token>. Unused when Ledger Token is empty. |
| Image List | No | none | Image List only. One image per line: team/app:1.0 or team/app@sha256:<hex>, optionally prefixed with the registry host. No tag means latest. # starts a comment. An image already exported at the same digest is skipped. |
| Initial Run | Yes | `Export All` | What a run does when no state exists yet. Default Export All: export every image found. Record Only: record the current digests and export nothing, so only later changes are exported. |
| Rewrite Target Base Path | No | none | Replace the first path segment of every target repository, which is the name Push creates: team/app becomes new/app with new. Empty (default): unchanged. Not with Strip Target Base Path. Environment variables allowed. |
| Strip Target Base Path | Yes | `false` | Remove the first path segment of every target repository: team/app becomes app. Default false: names unchanged. Not with Rewrite Target Base Path. |
| Tar Output Directory | Yes | none | Local directory on this node for the tars, such as /data/out. Set it from a parameter, #{container-images.tar.dir}. Use a per-node volume, same path on every node, outside NiFi's repositories. Trap: FetchFile reads it by path: keep the connection not load-balanced, or use shared storage. |
| Tar Filename Prefix | Yes | `container-images-` | Start of each tar's file name, followed by the UTC time and .tar, such as container-images-20260101T000000000000Z.tar. Default container-images-. Pull treats any such file left in Tar Output Directory as its own, so keep it unique there. |
| X-Artifact-Type Value | Yes | `container-images` | Value of the X-Artifact-Type attribute on the output FlowFile, which a RouteOnAttribute downstream matches. Default container-images, the value the Push route expects. Change it only together with that route. |
| Max Export Size | No | none | Most blob bytes in one tar, such as 20 GB; a layer shared by two images counts once. The rest go in the next run; an image over the limit goes alone. Empty (default): no limit. |
| Request Timeout | Yes | `600` | Seconds a registry read may stall before it fails. Default 600 (10 minutes). Blobs stream, so this is a stall limit, not a limit on the total time. |
<!-- /properties -->

**ExportContainerImages**

<!-- properties:ExportContainerImages -->
| Name | Required | Default | Description |
|---|---|---|---|
| Source Registry URL | Yes | none | Registry to read images from, such as https://registry.example.com, or https://host/artifactory/api/docker/repo-key for Artifactory. No user:password in the URL. Required, no default. Environment variables allowed: ${REGISTRY_URL}. |
| Source Registry Username | No | none | User for the source registry. Empty (default): anonymous access, which works only on a registry that allows it. Environment variables allowed: ${REGISTRY_USER}. |
| Source Registry Password | No | none | Password or access token for the source registry. Empty (default): none sent. Sensitive: reference a parameter, such as #{registry_password}. |
| SSL Context Service | No | none | A StandardSSLContextService whose PKCS12 or JKS truststore holds the registry's CA (it does not accept PEM). Its CAs add to the system store and SSL_CERT_FILE. Empty (default): only those are trusted. Its keystore is ignored. |
| Trusted Storage Hosts | No | none | Hosts, IPs or CIDR ranges the registry may redirect blob transfers to although they are private, such as an on-premises MinIO: minio.internal, 10.20.0.0/16. Empty (default): private, loopback, link-local and http-from-https redirects are refused. The registry's own host is always allowed. |
| Allowed Repositories | Yes | none | Allowlist, not the selection: the webhook body picks the images and each must be in here. Comma or new line separated: team/app, or team/* for a prefix, * for all. Required. Any other repository fails the FlowFile. |
| Tar Output Directory | Yes | none | Local directory on this node for the tars, such as /data/out. Set it from a parameter, #{container-images.tar.dir}. Use a per-node volume, same path on every node, outside NiFi's repositories. Trap: Push reads it by path: keep the connection not load-balanced, or use shared storage. |
| Tar Filename Prefix | Yes | `container-images-` | Start of each tar's file name, followed by the UTC time and .tar, such as container-images-20260101T000000000000Z.tar. Default container-images-. Pull treats any such file left in Tar Output Directory as its own, so keep it unique there. |
| X-Artifact-Type Value | Yes | `container-images` | Value of the X-Artifact-Type attribute on the output FlowFile, which a RouteOnAttribute downstream matches. Default container-images, the value the Push route expects. Change it only together with that route. |
| Max Export Size | No | none | Most blob bytes in one tar, such as 20 GB; a layer shared by two images counts once. More fails the whole webhook, nothing is split. Empty (default): no limit. |
| Tar Per Image | Yes | `false` | Default false: one tar for the webhook, shared layers once. true: one tar per image; layers repeat, so more bytes cross the diode (1.92x with 2 images sharing 4 of 5 layers). The FlowFile then holds one JSON line per tar, for SplitText. |
| Request Timeout | Yes | `600` | Seconds a registry read may stall before it fails. Default 600 (10 minutes). Blobs stream, so this is a stall limit, not a limit on the total time. |
<!-- /properties -->

**PushContainerImages**

<!-- properties:PushContainerImages -->
| Name | Required | Default | Description |
|---|---|---|---|
| Target Registry URL | Yes | none | Registry to push images into, such as https://registry.example.com, or https://host/artifactory/api/docker/repo-key for Artifactory. No user:password in the URL. Required, no default. FlowFile attributes allowed. |
| Target Registry Username | No | none | User for the target registry. Empty (default): anonymous access, which works only on a registry that allows it. Environment variables allowed: ${REGISTRY_USER}. |
| Target Registry Password | No | none | Password or access token for the target registry. Empty (default): none sent. Sensitive: reference a parameter, such as #{registry_password}. |
| SSL Context Service | No | none | A StandardSSLContextService whose PKCS12 or JKS truststore holds the registry's CA (it does not accept PEM). Its CAs add to the system store and SSL_CERT_FILE. Empty (default): only those are trusted. Its keystore is ignored. |
| Trusted Storage Hosts | No | none | Hosts, IPs or CIDR ranges the registry may redirect blob transfers to although they are private, such as an on-premises MinIO: minio.internal, 10.20.0.0/16. Empty (default): private, loopback, link-local and http-from-https redirects are refused. The registry's own host is always allowed. |
| Input Tar Path | No | none | Path of the export tar on this node's local disk, such as #{container-images.tar.dir}/${filename}. Export must write it here, so no load balancing. Never in NiFi's repositories. If missing, one <path>.pushing-* left by a kill is adopted. Empty (default): FlowFile content, in memory, small tars only. |
| Delete Input Tar | Yes | `false` | Delete the tar once every image is pushed. Default false: it stays. A failed push keeps it. The tar is renamed to <path>.pushing-<id> while pushing, so a killed push leaves that name; a replay adopts it. Ignored when Input Tar Path is empty. |
| Rewrite Target Base Path | No | none | Replace the first path segment of every repository pushed: team/app becomes new/app with new. Empty (default): the name recorded in the tar. Not with Strip Target Base Path. FlowFile attributes allowed. |
| Strip Target Base Path | Yes | `false` | Remove the first path segment of every target repository: team/app becomes app. Default false: names unchanged. Not with Rewrite Target Base Path. |
| Move Existing Tags | Yes | `false` | Let a push move a tag that already points at another digest in the target registry. Default false: such a tar fails with registry.error and nothing is pushed. A tag already at the right digest is skipped either way. |
| Request Timeout | Yes | `600` | Seconds a registry read or write may stall before it fails. Default 600 (10 minutes). Blobs stream, so this is a stall limit, not a limit on the total time. |
<!-- /properties -->

## Usage

Low side. Run Pull on the **primary node** only (Scheduling > Execution), every 15 minutes:

```text
PullContainerImages (Source Registry URL, Repositories to Export team/*, Tar Output Directory #{container-images.tar.dir}, + diode.route = container-images)
  -> FetchFile (File to Fetch ${absolute.path}${filename}, Completion Strategy Delete File)
  -> the diode
```

High side:

```text
RouteOnAttribute ${X-Artifact-Type:equals('container-images')}
  -> PutFile (Directory /data/in)
  -> PushContainerImages (Target Registry URL, Input Tar Path /data/in/${filename}, Delete Input Tar true)
     success -> done   failure -> a stopped processor, to inspect registry.error   original -> auto-terminate
```

Low side, driven by the gitlab-registry-mirror webhook (`MIRROR_WEBHOOK_URL`) instead of a schedule:

```text
ListenHTTP
  -> RouteOnAttribute ${X-Artifact-Type:equals('container-images-request')}
  -> ExportContainerImages (Source Registry URL, Allowed Repositories team/*, Tar Output Directory #{container-images.tar.dir})
  -> PushContainerImages (Target Registry URL, Input Tar Path #{container-images.tar.dir}/${filename}, Delete Input Tar true)   failure -> a stopped processor
```

The body only selects images. ExportContainerImages fails the FlowFile to `failure`, with `registry.error`, and exports nothing when: `X-Sha256` is missing or is not the sha256 of the body; the body is not JSON `{"registry", "images": [{"repository", "tag", "digest"}]}`; `registry` is not the host of Source Registry URL (`:443` is ignored); a repository is not in Allowed Repositories; a repository, tag or digest is malformed; or the digest the registry resolves for `repository:tag` now differs from the body's. Otherwise all images go into one tar, and `success` carries the same attributes as Pull's (empty content). To send it across a diode, put FetchFile after it as for Pull. Content-based integrity only: add a token on ListenHTTP if the sender must be authenticated.

#### Attributes on the Export success FlowFile

Every attribute of the incoming webhook FlowFile (for example `X-CI-Project`, `X-CI-Pipeline-Url`, `X-CI-Commit-Sha`, `X-Image-Count`, `X-Registry`) stays on the success FlowFile, because NiFi's Python bridge writes the processor's attributes onto the incoming FlowFile (`FlowFileTransformProxy.onTrigger` calls `session.putAllAttributes` on it) instead of creating a new one. Export sets or replaces only the ones below, so `X-Sha256` and `X-Artifact-Type` now describe the tar, not the webhook.

| Attribute | Value |
|---|---|
| `filename`, `Filename`, `absolute.path` | The tar and the directory it is in |
| `X-Sha256`, `file.size` | The tar's sha256 and size |
| `X-Artifact-Type`, `X-Artifact-Format` | X-Artifact-Type Value, and `tar` |
| `registry.host` | The host of Source Registry URL |
| `registry.images` | One `repository:tag@digest` per line, in body order |
| `registry.image.count` | The number of images |
| `registry.image.N.repository`, `.tag`, `.digest` | Image N, counting from 0 in body order |

The `X-CI-*`, `X-Image-Count` and `X-Registry` headers are for routing and display only. Export never reads them to select or check images: the body, `X-Sha256` and the digest the registry resolves now are the only authority, so a header that disagrees with the body changes nothing.

#### One tar per image

Tar Per Image `false` (default) writes one tar for the whole webhook, with shared layers stored once. `true` writes one tar per image, so shared base layers repeat in each and more bytes cross the diode: measured with 2 images sharing 4 of 5 layers, 1.92 times the bytes of one tar. That is the trade-off for routing, retrying and tracking each image alone. Max Export Size counts each distinct blob digest once per tar, so in one-tar mode a layer shared by two images counts once (a 48.3 MB bundle passes a 60 MB limit) and with `true` each image is measured on its own. All checks run on the whole webhook first, and if any image or any write fails in a way Export handles, nothing is left in Tar Output Directory. That holds only for handled failures: after a crash or kill Export leaves `.<name>.partial` and Push leaves `<name>.pushing-<id>`. Delete them by hand, or run the cleanup flow under "Clean up leftover files" below.

A Python FlowFileTransform returns exactly one FlowFile, so with `true` the success FlowFile has empty `filename` and `absolute.path` and holds one JSON object per tar, one per line (`mime.type` `application/x-ndjson`). Each object carries `filename`, `absolute.path`, `X-Sha256`, `file.size`, `registry.host`, `registry.repository`, `registry.tag`, `registry.digest` and `registry.image.count` `1` with `registry.image.0.*` for that image. Other attributes on the FlowFile: `registry.tar.count`, and the whole-webhook `registry.images` and `registry.image.*`. Turn each line into a FlowFile:

```text
ExportContainerImages (Tar Per Image true)
  -> SplitText (Line Split Count 1, Header Line Count 0)
  -> EvaluateJsonPath (Destination flowfile-attribute, Return Type auto-detect, and one property per attribute:
       filename = $['filename'], X-Sha256 = $['X-Sha256'], registry.repository = $['registry.repository'],
       registry.tag = $['registry.tag'], registry.digest = $['registry.digest'])
  -> PushContainerImages (Input Tar Path #{container-images.tar.dir}/${filename})
```

Each Push FlowFile then pushes one image. The split FlowFiles also carry the whole-webhook attributes, so read `registry.repository`, `registry.tag` and `registry.digest` for the image, not `registry.image.*`.

A FlowFile out of Push still holds the tar. Auto-terminate `success`, or give its queue a back-pressure size above the largest tar; a full queue stops Push.

## Behaviour

- Pull keeps `repository:tag -> digest` (16 hex characters of it) as one compressed string cut into cluster-state values of 60,000 bytes (`exported.0`, `exported.1`, ...), because ZooKeeper stores each value with `writeUTF`, which takes at most 65,535 bytes. It is pruned to what the source still lists, about 30 bytes a tag against the roughly 1 MB ZooKeeper allows. The pending record of an export (below) costs about the same per tag again, so one first export of a very large listing can use twice that. Past 900 KB the run fails and says to set Max Export Size or narrow Repositories to Export, before it writes a tar. State from 1.4.x (one value, `exported`) still loads and is rewritten in the new form. The state is written only after the tar is complete. The tar is written as a hidden `.<name>.partial` and renamed when done. A failed run leaves no tar and no state change. After a crash or kill, `.<name>.partial` stays until deleted.
- NiFi 2.11 creates and commits Pull's FlowFile only after `create()` has returned and offers no hook after that, so the state is already written when a crash or a failed commit can still lose the FlowFile. Pull therefore emits a FlowFile again for any finished `<Tar Filename Prefix>*.tar` still in Tar Output Directory after an hour (`LEFTOVER_AFTER` in `PullContainerImages.py`), oldest first and one per run, before it exports anything else. FetchFile with Completion Strategy `Delete File` removes a tar once fetched, so only a tar that never reached FetchFile comes back. Push skips tags that are already present, so a repeat is harmless; a tar fetched slower than an hour can be emitted twice, and the second FetchFile then fails with not found.
- When the primary node changes, the tar of a lost FlowFile is on the old node's disk. Pull therefore keeps, next to `exported`, a `pending` record of each export (node, tar name, and each image's repository, tag and 16 hex characters of its digest) in cluster state until the node that wrote the tar finds it fetched. A new primary that finds a record of another node older than an hour with no such tar in its own Tar Output Directory exports those images again at the digest the registry has for the tag now (images a later export has moved on, or that are gone, are left out; one that cannot be read from the registry this run waits for the next regular run) and records that export in turn. Nothing here can tell a tar fetched just before the failover from one lost with it, so the worst case is one repeated export, which Push takes as already present.
- One tar per run. With Max Export Size, the images left over go on the next run. Max Export Size counts each distinct blob digest once per tar, so a layer shared by two images counts once.
- Pull's FlowFile has no content. Its attributes: `filename`, `Filename`, `absolute.path`, `Content-Type`, `X-Sha256`, `X-Artifact-Type`, `X-Artifact-Format`, `registry.host`, `registry.images`, `file.size`, and the dynamic properties.
- Blobs stream between registry and disk in 8 MiB chunks and are checked against their digests. Memory stays flat whatever the image size. Everything else a registry sends is read with a cap and refused beyond it: 4 MiB for a manifest, 16 MiB for a token, listing or upload answer, 64 MiB for the ledger; an error body is read for its first 300 bytes only.
- Push retries a failed blob upload twice from the tar, authorizing again after a 401. With Delete Input Tar it renames the tar while it works, so two tasks given the same path cannot both push it; a tar it cannot delete leaves the FlowFile on `success` with `registry.tar.leftover`. After a kill mid-push the tar stays as `<path>.pushing-<id>`; when the replayed FlowFile finds Input Tar Path missing and exactly one such sibling, Push adopts it and continues, and more than one is an error naming them all. Push holds an exclusive `flock` on the tar for the whole push, and the adopter skips any leftover it cannot lock, so a push still running is never taken. The lock needs a filesystem that honours flock. Local disk works, and the CephFS kernel client is proven across nodes (2026-10-14: a lock held on one node blocked a probe on that node and on a second node, and the second node got it after release). NFS is untested (NFSv4 maps flock to fcntl locks); where locks are not honoured, do not run two Push tasks on one directory.
- Two images of one tar that end at the same repository and tag after Rewrite or Strip Base Path (`a/app:v1` and `b/app:v1` under Rewrite `merged`) fail the tar with nothing pushed, whatever Move Existing Tags says; Pull leaves the second one out of its export and logs why, in every later run too while the first stays in its state at another digest.
- An index may hold other indexes (up to 8 levels). Pull exports every manifest and blob reachable from it, and Push sends each manifest before the index that names it. A deeper graph is skipped by Pull with a warning.
- Every config and layer blob is copied, whatever `urls` its descriptor lists. Only a foreign layer (Docker `rootfs.foreign` or OCI `nondistributable` media type) that has `urls` is left to those urls, as before.
- Push checks every target tag before the first upload, skips blobs the registry holds, mounts a blob shared across repositories, and reads each tag back. Its success FlowFile carries `registry.images`, `registry.pushed` and `registry.present`.
- The registry URL must not contain a user name or password (use the registry username and password properties). Error text, `registry.error` and the log show a URL as scheme, host and path only, so a pre-signed storage signature or embedded credential is never written there.
- Catalog and tag listings follow the `rel=next` link of a `Link` header to the end, whatever order its parameters come in.
- A redirect to object storage is followed without the registry's credentials, and so is an upload Location on another host. Only http and https are followed, an https registry is never sent to plain http, and a host that resolves to a loopback, link-local (cloud metadata), private or other non-public address is refused. A registry whose object storage is on a private address (an on-premises MinIO) needs that host or range in **Trusted Storage Hosts** on Pull, Export and Push, for example `minio.internal, 10.20.0.0/16`. The registry's own host is allowed at every address its name resolved to at the first connection (a registry with several addresses answers on all of them). A later connection to that name (a redirect or upload Location on the same host) at an address the name was not seen to resolve to is sent without the registry's credentials, and only if it passes the same checks as object storage, so a name re-pointed after the first answer gets no credentials.

## Environment variables

Source Registry URL, Source and Target Registry Username, Pull's Repositories to Export, Ledger URL, Image List and Rewrite Target Base Path, Export's Allowed Repositories, and Tar Output Directory are evaluated against the NiFi environment, so `${REGISTRY_HOST}` or `https://${REGISTRY_HOST}:5000` read the environment variable of that name. NiFi 2.11's Python API answers a plain `${NAME}` from the attribute map alone, so `nifi_attributes.py` puts the environment behind that map. Target Registry URL, Input Tar Path and Rewrite Target Base Path on Push read FlowFile attributes instead.

## Upgrading from 1.4.x or earlier

Property names changed and CA Certificate File was removed (see the migration table in the top-level README). A processor on 1.4.x keeps working until you change its version; then set each property again under its new name.

**CA Certificate File was removed** from Pull and Push, so a flow that set it no longer trusts that CA (Pull would carry the old value as a dynamic attribute; delete it) until you set **SSL Context Service** instead: a StandardSSLContextService (or any SSLContextProvider) whose truststore holds the CAs. The processors read the CA certificates from that truststore and trust them in addition to the system store. A keystore in the service is not used (no client certificates). Flows pinned to 1.4.0 keep working until you change the version.

## Where to put the tar directory

Set Tar Output Directory (and the directory part of Input Tar Path on Push) from a parameter in a parameter context, for example `#{container-images.tar.dir}` with the value `/data/out`. Changing the value then needs no NiFi restart: NiFi stops the processors that use the parameter, applies it and starts them again.

- **Recommended:** a dedicated volume on each node, mounted at the same path on every node, used for nothing else. Leave the connection from Export to Push on Load Balance Strategy `Do not load balance` (the default): the FlowFile then stays on the node where Export wrote the tar, and Push on that node reads its own disk. Shared storage is not required.
- **Shared storage (ReadWriteMany, NFS):** only needed when that connection is load-balanced, because Push may then run on a node that did not write the tar.
- **Never** inside the content repository, FlowFile repository or provenance repository directories. NiFi leaves a tar there alone while it runs, but on restart its FileSystemRepository startup scan deletes every file it did not write (archive disabled) or tries to archive it (archive enabled). A tar in those trees is lost or moved at the next restart.
- **Why not FlowFile content:** the Python API holds content in memory (`getContentsAsBytes()`, and `contents` as one `bytes` object), so a multi-GB tar cannot travel as FlowFile content.

### If your only volume is the content repository PVC

1. **Keep the tars out of NiFi's tree.** Cleanest: set `nifi.content.repository.directory.default` to a subfolder (for example `<mount>/repo`) and put the tars in a sibling subfolder (`<mount>/container-images`). Changing `nifi.content.repository.directory.default` does not move existing content, and queued FlowFiles then fail with ContentNotFoundException. Before starting NiFi on the new path, copy the old directory into it with `cp -a <old>/. <new>/`, or drain every queue first. The alternative is a subfolder inside the current mount, which needs a restart test in a lab first.
2. **Respect the space rule.** From the NiFi 2.x Administration Guide: archive cleanup starts at `nifi.content.repository.archive.max.usage.percentage` (default `50%`), and content writes are refused at `nifi.content.repository.archive.backpressure.percentage` (when unset, 2 points above the first, so `52%`) until usage drops back below the first. Both measure the whole volume, tars included. Tars are not archive, so archive cleanup cannot free them. Above the backpressure percentage content writes block until the tars are deleted, and at 100% they fail with "No space left on device". Cap them with Max Export Size and 1 concurrent task on Export, so the peak is about 2 x Max Export Size of tars per node (one being written, one waiting for Push). With Tar Per Image `true`, one webhook writes one tar per image at once, so count the images. Keep normal peak content plus that under 50% of the volume.
   Worked example: a 200 GB PVC, Max Export Size 20 GB. Tars peak at 2 x 20 = 40 GB. The 50% line is 100 GB, so normal content must stay under 60 GB at its peak. The 52% line, where writes stop, is 104 GB.
3. **Clean up leftover files in NiFi.** A tar whose Push failed stays on disk, and a crash or kill leaves `.<name>.partial` (Export) or `<name>.pushing-<id>` (Push). Add ListFile on the tar directory with Minimum File Age `1 day` (for `.partial` files also set Ignore Hidden Files `false` and a File Filter that matches them, since ListFile skips hidden files by default), then FetchFile with Completion Strategy `Delete File`, then auto-terminate. Or delete them by hand. Keep Delete Input Tar `true` on Push so a good tar is removed once its images are pushed.

## Cluster behaviour of Tar Output Directory

Pull and Export write the tar to the local disk of the node they run on; Push (Input Tar Path) and FetchFile read it by path. NiFi 2.x Python processors return FlowFile content as one `bytes` object and read it with `getContentsAsBytes()`; there is no streaming, so a multi-GB tar is not carried as FlowFile content. In a cluster run both ends on the same node, or make the directory shared storage mounted at the same path on every node.

## Preconditions

- Tar Output Directory and the PutFile directory exist on every node and are writable by the NiFi user.
- `SSL_CERT_FILE` names the CA bundle, such as the cert-manager `ca.crt`, or SSL Context Service is set.

## Expected result

```sh
curl -s -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json' \
  https://registry.high.example.internal/v2/team/app/manifests/1.0 | sha256sum
```

The digest matches the one in `registry.images`.
