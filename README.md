# nifi-container-registry-processors

NiFi Python processors that export container images from a registry into a tar and push such a tar into a registry, digests unchanged, with no skopeo or docker on the NiFi host. The tar is the export format of mirror.py (djlongy/gitlab-registry-mirror), so either side can be NiFi or mirror.py.

The Python processors in `python/container_registry/` are the maintained implementation. They need no build and no dependencies; see that directory's README for installing and operating them.

The earlier NAR releases (v1.x on GitHub) are no longer maintained. The Python processors replace them; see the [migration table](#upgrading-from-14x-or-earlier-renamed-and-removed-properties) for renamed and removed properties.

## More documentation

For **PLANA native layer transfer**, use the [complete by-hand build with low/high canvas screenshots](docs/runbooks/nifi-by-hand-compact-layer-flow.md). It includes every property, relationship, connection and recovery step.

[docs/README.md](docs/README.md) maps the decision records (why Python, where the tar lives, parameters, one tar or many, webhook trust), the click-by-click runbook, the testing checklist and the attribute reference.

## Requirements

- Apache NiFi 2.x with Python enabled (`nifi.python.command`), Python 3.10 or later on the node. The processors use the Python standard library only.
- Any registry that follows the distribution API: `registry:2` (open, htpasswd or token auth), Quay, Artifactory, Harbor, GitLab.

## Export format

The tar both processors read and write, and `mirror.py export` and `import` use:

```text
export.json            {"images": [{"dir": "000000", "source": "...", "target": "team/app", "tag": "1.0", "digest": "sha256:..."}]}
000000/manifest.json   the image's manifest or index, byte for byte (its sha256 is the digest)
000000/<hex>.manifest.json   each manifest below an index, nested indexes included
000000/<hex>           each config and layer blob; one shared with an earlier directory is a hard link to it
000000/version         Directory Transport Version: 1.1 (the skopeo dir: layout)
```

A change to this layout is a new major version.

## Python processors 1.5.0

`python/container_registry/` holds PullContainerImages and PushContainerImages in Python, plus ExportContainerImages (details in its README). They need no build: copy the directory into the Python extensions directory of every node. To go from nothing to one pushed image, follow [QUICKSTART.md](QUICKSTART.md); for a click-by-click build of the webhook flow in the NiFi UI, see [docs/runbooks/nifi-by-hand.md](docs/runbooks/nifi-by-hand.md).

| Processor | Side | What it does |
|---|---|---|
| PullContainerImages | source | Reads a source registry on a schedule (primary node only) and writes one export tar of what changed to Tar Output Directory, then emits an empty FlowFile naming it, for FetchFile |
| ExportContainerImages | source | Takes a gitlab-registry-mirror webhook as a FlowFile and writes one export tar of the images it names to Tar Output Directory |
| PlanRegistryTransfer | source | Plans a layer-level transfer: reads manifests only and emits one JSON job list (config, layers, manifests, index) that native processors act on. See [docs/runbooks/nifi-by-hand-native-layer-flow.md](docs/runbooks/nifi-by-hand-native-layer-flow.md) |
| PushContainerImages | target | Pushes every image of an export tar into the target registry, digests unchanged, and reads each tag back |

Property names say which registry they belong to: `Source Registry ...` on Pull and Export, `Target Registry ...` on Push. Rewrite Target Base Path and Strip Target Base Path change the repository name the images get on the target, whichever processor applies them.

### Flows

Diode, scheduled:

```text
PullContainerImages -> FetchFile (Completion Strategy Delete File) -> the diode
the diode -> RouteOnAttribute ${X-Artifact-Type:equals('container-images')}
  -> PutFile (Directory /data/in) -> PushContainerImages (Input Tar Path /data/in/${filename}, Delete Input Tar true)
```

Webhook, driven by gitlab-registry-mirror (`MIRROR_WEBHOOK_URL`):

```text
ListenHTTP -> RouteOnAttribute ${X-Artifact-Type:equals('container-images-request')}
  -> ExportContainerImages -> PushContainerImages (Input Tar Path #{container-images.tar.dir}/${filename}, Delete Input Tar true)
```

A webhook whose `X-Sha256` does not match its body, whose registry host, repositories or digests do not check out, fails with `registry.error` and exports nothing.

### Attributes on the Export success FlowFile

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

### Moving large images: options

The NiFi 2.x Python API cannot stream: content is one `bytes` object and about 13 times its size in RAM, with a 2 GiB hard limit. [ADR 0006](docs/decisions/adr-0006-moving-large-images-through-nifi.md) records the evidence. Each option keeps layer bytes out of Python memory.

| Option | Use when | Evidence | By hand |
|---|---|---|---|
| O1 Staging dir + FetchFile (these processors) | Default for tar-shaped flows | 3 GiB, lab flow | [nifi-by-hand.md](docs/runbooks/nifi-by-hand.md) |
| O2 One FlowFile per layer (InvokeHTTP) | Exact digests, per-layer resend, a diode that carries many files | 1 GiB, 3-node cluster | [O2](docs/runbooks/nifi-by-hand-o2-layer-flow.md) |
| O3 Loopback ListenHTTP with mTLS | No staging disk allowed | 1 GiB, 3-node cluster | [O3](docs/runbooks/nifi-by-hand-o3-loopback.md) |
| O4 Push from stdin ([push_from_stdin.py](python/scripts/push_from_stdin.py)) | Push a layer or manifest FlowFile with no Python processor | 3 GiB lab, 5 MB cluster | [O4](docs/runbooks/nifi-by-hand-o4-push.md) |

The diode high side uses the O2 transfer with the O4 push. Validate the chosen option before a release.

### One tar per image

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

### Where to put the tar directory

Set Tar Output Directory (and the directory part of Input Tar Path on Push) from a parameter in a parameter context, for example `#{container-images.tar.dir}` with the value `/data/out`. Changing the value then needs no NiFi restart: NiFi stops the processors that use the parameter, applies it and starts them again.

- **Recommended:** a dedicated volume on each node, mounted at the same path on every node, used for nothing else. Leave the connection from Export to Push on Load Balance Strategy `Do not load balance` (the default): the FlowFile then stays on the node where Export wrote the tar, and Push on that node reads its own disk. Shared storage is not required.
- **Shared storage (ReadWriteMany, NFS):** only needed when that connection is load-balanced, because Push may then run on a node that did not write the tar.
- **Never** inside the content repository, FlowFile repository or provenance repository directories. NiFi leaves a tar there alone while it runs, but on restart its FileSystemRepository startup scan deletes every file it did not write (archive disabled) or tries to archive it (archive enabled). A tar in those trees is lost or moved at the next restart.
- **Why not FlowFile content:** the Python API holds content in memory (`getContentsAsBytes()`, and `contents` as one `bytes` object), so a multi-GB tar cannot travel as FlowFile content.

#### If your only volume is the content repository PVC

1. **Keep the tars out of NiFi's tree.** Cleanest: set `nifi.content.repository.directory.default` to a subfolder (for example `<mount>/repo`) and put the tars in a sibling subfolder (`<mount>/container-images`). Changing `nifi.content.repository.directory.default` does not move existing content, and queued FlowFiles then fail with ContentNotFoundException. Before starting NiFi on the new path, copy the old directory into it with `cp -a <old>/. <new>/`, or drain every queue first. The alternative is a subfolder inside the current mount, which needs a restart test in a lab first.
2. **Respect the space rule.** From the NiFi 2.x Administration Guide: archive cleanup starts at `nifi.content.repository.archive.max.usage.percentage` (default `50%`), and content writes are refused at `nifi.content.repository.archive.backpressure.percentage` (when unset, 2 points above the first, so `52%`) until usage drops back below the first. Both measure the whole volume, tars included. Tars are not archive, so archive cleanup cannot free them. Above the backpressure percentage content writes block until the tars are deleted, and at 100% they fail with "No space left on device". Cap them with Max Export Size and 1 concurrent task on Export, so the peak is about 2 x Max Export Size of tars per node (one being written, one waiting for Push). With Tar Per Image `true`, one webhook writes one tar per image at once, so count the images. Keep normal peak content plus that under 50% of the volume.
   Worked example: a 200 GB PVC, Max Export Size 20 GB. Tars peak at 2 x 20 = 40 GB. The 50% line is 100 GB, so normal content must stay under 60 GB at its peak. The 52% line, where writes stop, is 104 GB.
3. **Clean up leftover files in NiFi.** A tar whose Push failed stays on disk, and a crash or kill leaves `.<name>.partial` (Export) or `<name>.pushing-<id>` (Push). Add ListFile on the tar directory with Minimum File Age `1 day` (for `.partial` files also set Ignore Hidden Files `false` and a File Filter that matches them, since ListFile skips hidden files by default), then FetchFile with Completion Strategy `Delete File`, then auto-terminate. Or delete them by hand. Keep Delete Input Tar `true` on Push so a good tar is removed once its images are pushed.

### Tar Output Directory and clusters

Export and Pull write the tar to the local disk of the node that runs them, and Push or FetchFile read it by path. NiFi 2.x Python processors return FlowFile content as a whole `bytes` object (`FlowFileTransformResult.contents`, `FlowFileSourceResult.contents`) and read it with `getContentsAsBytes()`, with no streaming, so a multi-GB tar is not moved through FlowFile content. In a cluster, keep Export and Push (or Pull and FetchFile) on the same node by leaving the connection between them on Load Balance Strategy `Do not load balance` (the default), or make Tar Output Directory shared storage mounted at the same path on every node.

### Properties

Generated from the descriptors; `python3 python/tests/test_readme_tables.py --write` rewrites them, and the unit tests fail when they drift.

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

**PlanRegistryTransfer**

<!-- properties:PlanRegistryTransfer -->
| Name | Required | Default | Description |
|---|---|---|---|
| Source Registry URL | Yes | none | Registry to read manifests from, such as https://registry.example.com, or https://host/artifactory/api/docker/repo-key for Artifactory. No user:password in the URL. Required, no default. Environment variables allowed: ${REGISTRY_URL}. |
| Source Registry Username | No | none | User for the source registry. Empty (default): anonymous access, which works only on a registry that allows it. Environment variables allowed: ${REGISTRY_USER}. |
| Source Registry Password | No | none | Password or access token for the source registry. Empty (default): none sent. Sensitive: reference a parameter, such as #{registry_password}. |
| SSL Context Service | No | none | A StandardSSLContextService whose PKCS12 or JKS truststore holds the registry's CA (it does not accept PEM). Its CAs add to the system store and SSL_CERT_FILE. Empty (default): only those are trusted. Its keystore is ignored. |
| Allowed Repositories | Yes | none | Allowlist, not the selection: the FlowFile picks the images and each must be in here. Comma or new line separated: team/app, or team/* for a prefix, * for all. Required. Any other repository fails the FlowFile. |
| Platform Selection | Yes | `all` | all (default): every platform, index kept, so its digest is the source's. Or platforms such as linux/amd64, linux/arm64/v8 (comma or new line separated): only those are planned, without the index; a single match gets the tag, on its own digest. |
| Maximum Manifest Depth | Yes | `16` | Most indexes nested in one another before an image is refused as malformed. Default 16. |
| Maximum Jobs Per Transfer | Yes | `1000` | Most jobs (configs, layers, manifests and indexes, a blob shared by platforms counted once) one image may plan. More fails the FlowFile. Default 1000. |
| Request Timeout | Yes | `60` | Seconds a registry read may stall before it fails. Default 60. Only manifests and HEAD checks are read here, never blobs. |
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
| Move Existing Tags | Yes | `true` | Move a tag that already points at another digest in the target registry, as docker push does. Default true. Set false to protect tags: such a tar then fails with registry.error and nothing is pushed. A tag already at the right digest is skipped either way. |
| Request Timeout | Yes | `600` | Seconds a registry read or write may stall before it fails. Default 600 (10 minutes). Blobs stream, so this is a stall limit, not a limit on the total time. |
<!-- /properties -->

### Upgrading from 1.4.x or earlier: renamed and removed properties

Pull and Push renamed and removed properties (breaking). A flow on 1.4.x keeps working until you change the processor version; then set each property again under its new name. ExportContainerImages is new in 1.5.0, so it has nothing to migrate.

| Processor | Old name | New name |
|---|---|---|
| Pull | Registry URL | Source Registry URL |
| Pull | Username | Source Registry Username |
| Pull | Password | Source Registry Password |
| Push | Registry URL | Target Registry URL |
| Push | Username | Target Registry Username |
| Push | Password | Target Registry Password |
| Pull | Repositories | Repositories to Export |
| Pull | Output Directory | Tar Output Directory |
| Pull | Filename Prefix | Tar Filename Prefix |
| Pull | Artifact Type | X-Artifact-Type Value |
| Push | Tar File | Input Tar Path |
| Push | Delete Tar File | Delete Input Tar |
| Pull, Push | Rewrite Base Path | Rewrite Target Base Path |
| Pull, Push | Strip Base Path | Strip Target Base Path |
| Pull, Push | CA Certificate File | removed: set SSL Context Service (a StandardSSLContextService with a PKCS12 or JKS truststore) |

All other names are unchanged.

## Changing the processors

The processors are plain Python so that a person, or a local model, can read, change and test them without compiling anything. Follow these steps in order.

### Layout (`python/container_registry/`)

| File | Holds |
|---|---|
| `PullContainerImages.py` | PullContainerImages: reads a registry on a schedule, writes tars, keeps state |
| `ExportContainerImages.py` | ExportContainerImages: checks a webhook, writes a tar (or one per image) |
| `PushContainerImages.py` | PushContainerImages: pushes a tar into a registry |
| `container_registry_client.py` | Everything shared: the registry client, tar writing and reading, size parsing, the attributes of a tar |
| `nifi_attributes.py` | Expression Language lookups (`evaluate`, `Attributes`) that also read environment variables |

Two rules, both checked by `python/tests/test_container_registry.py`:

- **A processor file never imports another processor file.** NiFi loads each file on its own, so an import of a sibling processor fails in NiFi even though it works in a test run. Shared code goes in `container_registry_client.py`.
- **Every PropertyDescriptor is a literal in the class body, with a literal `name=`** (`REGISTRY_URL = PropertyDescriptor(name="Source Registry URL", ...)`). NiFi finds the properties by reading the source with Python's abstract syntax tree and does not run it (Python Developer Guide), so a descriptor built in a loop, a function or another class is invisible in the UI. A dependency (`PropertyDependency`) must name its descriptor as a plain variable, not `self.X`.

Use only the Python standard library. `python/requirements.txt` lists no packages, so NiFi never installs anything for these processors.

### Test without NiFi

```sh
python3 -m unittest discover -s python/tests
```

`nifi_stub.py` stands in for the `nifiapi` package, and `fake_registry.py` is an in-memory registry, so no NiFi, network or container runtime is needed. Add a test next to the nearest existing one in `python/tests/` for every behaviour you change. If you change a property name, default or description, regenerate the tables in the READMEs, or the tests fail:

```sh
python3 python/tests/test_readme_tables.py --write
```

### Load a change into a running NiFi 2.x

1. Copy the changed files over the ones in the Python extensions directory of the node (`cp -r python/container_registry <python extensions directory>/`).
2. Stop the processor and start it again. Per NiFi's Python Developer Guide ("Processor Reloading"), a changed source takes effect when the processor is next started, with no NiFi restart. Stopped processors may also reload on validation.
3. If the processor never loaded successfully before, NiFi may not watch it for changes: restart NiFi.
4. When behaviour changes, raise `version` in the processor's `ProcessorDetails` (and the same number in `python/container_registry/README.md`), so a flow can tell the versions apart. NiFi can hold two versions of one processor side by side; choose one with **Change Version** on the processor.

## Behaviour

- The processors read and write the mirror.py export layout: `export.json` plus one `skopeo dir:` copy per image, shared blobs stored once as hard links. `mirror.py import` takes a tar PullContainerImages wrote, and PushContainerImages takes one `mirror.py export` wrote.
- Pull's output carries the attributes `mirror.py send` uses: `filename`, `Filename`, `Content-Type`, `X-Sha256`, `X-Artifact-Type` (`container-images`), `X-Artifact-Format` (`tar`), plus `registry.images`.
- Pull resolves each tag to a digest once and copies that digest, so a tag that moves mid-run is taken next run. A digest the registry no longer holds is skipped with a warning and retried. Any other error emits nothing and records nothing.
- Pull keeps `repository:tag -> digest` in cluster state and records it only once the tars are committed. A rebuilt low registry with the same images exports nothing; clearing the state (**View State > Clear**) exports everything again.
- With `Ledger`, the ledger is read once per run and refused when it names another registry than Source Registry URL. It is never written.
- Push checks every image against its recorded digest and every target tag before the first upload, skips tags already at their digest and blobs the registry holds, uploads a blob shared by several repositories once and mounts it into the rest, and reads each tag back after the push.

## Preconditions

- The push account may create repositories, or they exist. Quay: a robot with write on the repository or creator on the organization.
- Artifactory: use the per-repository API base `https://<host>/artifactory/api/docker/<repo-key>` as Source Registry URL or Target Registry URL. Its repository-path method answers uploads with a Location that drops the repository key.
- The registries, and the Ledger URL, are reachable from every NiFi node.

## Out of scope

- Signing or verifying signatures; scanning.
- Deleting tags or repositories.
- Splitting one image across tars.

## Expected result

Push routes to `success` with `registry.pushed` and `registry.present`; Pull emits `container-images-<UTC time>.tar`. Check a tag on the far side:

```sh
skopeo inspect --raw docker://registry.high.example.internal/team/app:1.0 | sha256sum
```
