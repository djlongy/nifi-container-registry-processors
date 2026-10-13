# container_registry

PushContainerImages, PullContainerImages and ExportContainerImages as NiFi Python processors: same tar, same attributes as the NAR, plain source.

## Requirements

- Apache NiFi 2.x with Python enabled (`nifi.python.command`), Python 3.10 or later on the NiFi host.
- Nothing from PyPI. `python/requirements.txt` lists no packages and sits outside this directory, so NiFi never runs `uv` for it and `UV_INDEX_URL` is not consulted.
- Disk on every node for the largest export, twice: the tar Pull writes, and the tar Push reads.

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

NiFi discovers the processors within a minute, listed as `PullContainerImages 2.0.0`, `PushContainerImages 2.0.0` and `ExportContainerImages 2.0.0` (bundle `python-extensions`). An edited file reloads when the processor next starts.

## Properties

The full set, with descriptions, is in `PullContainerImages.py`, `PushContainerImages.py` and `ExportContainerImages.py` and on each processor's Usage page.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | Registry URL | none | `https://host`, `http://host:5000`, or Artifactory's `https://host/artifactory/api/docker/<repo-key>` |
| Optional | Username, Password | none | Empty for an open registry |
| Optional | SSL Context Service | none | An SSLContextProvider whose truststore CAs are trusted in addition. The system store and `SSL_CERT_FILE` are always trusted |
| Optional | Trusted Storage Hosts | none | Hosts, addresses or CIDR ranges (`minio.internal, 10.20.0.0/16`) the registry may redirect blobs to although they are private. Loopback, link-local, private and plain-http-from-https targets are refused otherwise |
| Optional | Rewrite Base Path, Strip Base Path | none, `false` | Replace or remove the first path segment of every repository |
| Pull | Image Source | `Registry Listing` | `Registry Listing` (**Repositories**, `team/*`, `*`), `Ledger` (**Ledger URL**), or `Image List` (**Image List**, one image per line) |
| Pull | Output Directory | none | Where each tar is written; FetchFile reads it from there |
| Pull | Initial Run | `Export All` | `Record Only` baselines the state and exports only later changes |
| Pull | Max Export Size | no limit | Blob bytes per tar. What does not fit goes on the next run |
| Pull | *(dynamic)* | none | Becomes an attribute on each FlowFile, Expression Language over its attributes |
| Export | Repositories | none (required) | Allowlist, comma or new line separated. An entry ending in `*` is a prefix (`team/*`); any other repository in the webhook fails it |
| Export | Output Directory, Filename Prefix, Artifact Type, Max Export Size, Request Timeout | as Pull | The tar is written there. Over Max Export Size fails the whole webhook, nothing is split |
| Push | Tar File | FlowFile content | Path of the tar on disk, such as `/data/in/${filename}`. Content is read into memory, so set this for any real tar |
| Push | Delete Tar File | `false` | Remove Tar File once every image is pushed |
| Push | Move Existing Tags | `false` | A tag at another digest fails the tar, and nothing is pushed, unless `true` |

## Usage

Low side. Run Pull on the **primary node** only (Scheduling > Execution), every 15 minutes:

```text
PullContainerImages (Repositories team/*, Output Directory /data/out, + diode.route = container-images)
  -> FetchFile (File to Fetch ${absolute.path}${filename}, Completion Strategy Delete File)
  -> the diode
```

High side:

```text
RouteOnAttribute ${X-Artifact-Type:equals('container-images')}
  -> PutFile (Directory /data/in)
  -> PushContainerImages (Tar File /data/in/${filename}, Delete Tar File true)
     success -> done   failure -> a stopped processor, to inspect registry.error   original -> auto-terminate
```

Low side, driven by the gitlab-registry-mirror webhook (`MIRROR_WEBHOOK_URL`) instead of a schedule:

```text
ListenHTTP
  -> RouteOnAttribute ${X-Artifact-Type:equals('container-images-request')}
  -> ExportContainerImages (Registry URL, Repositories team/*, Output Directory /data/out)
  -> PushContainerImages (Tar File /data/out/${filename}, Delete Tar File true)   failure -> a stopped processor
```

The body only selects images. ExportContainerImages fails the FlowFile to `failure`, with `registry.error`, and exports nothing when: `X-Sha256` is missing or is not the sha256 of the body; the body is not JSON `{"registry", "images": [{"repository", "tag", "digest"}]}`; `registry` is not the host of Registry URL (`:443` is ignored); a repository is not in Repositories; a repository, tag or digest is malformed; or the digest the registry resolves for `repository:tag` now differs from the body's. Otherwise all images go into one tar, and `success` carries the same attributes as Pull's (empty content). To send it across a diode, put FetchFile after it as for Pull. Content-based integrity only: add a token on ListenHTTP if the sender must be authenticated.

A FlowFile out of Push still holds the tar. Auto-terminate `success`, or give its queue a back-pressure size above the largest tar; a full queue stops Push.

## Behaviour

- Pull keeps `repository:tag -> digest` (16 hex characters of it) as one compressed string cut into cluster-state values of 60,000 bytes (`exported.0`, `exported.1`, ...), because ZooKeeper stores each value with `writeUTF`, which takes at most 65,535 bytes. It is pruned to what the source still lists, about 30 bytes a tag against the roughly 1 MB ZooKeeper allows. The pending record of an export (below) costs about the same per tag again, so one first export of a very large listing can use twice that. Past 900 KB the run fails and says to set Max Export Size or narrow Repositories, before it writes a tar. State from 1.4.0 (one value, `exported`) still loads and is rewritten in the new form. The state is written only after the tar is complete. The tar is written as a hidden `.<name>.partial` and renamed when done. A failed run leaves no tar and no state change.
- NiFi 2.11 creates and commits Pull's FlowFile only after `create()` has returned and offers no hook after that, so the state is already written when a crash or a failed commit can still lose the FlowFile. Pull therefore emits a FlowFile again for any finished `<Filename Prefix>*.tar` still in Output Directory after an hour (`LEFTOVER_AFTER` in `PullContainerImages.py`), oldest first and one per run, before it exports anything else. FetchFile with Completion Strategy `Delete File` removes a tar once fetched, so only a tar that never reached FetchFile comes back. Push skips tags that are already present, so a repeat is harmless; a tar fetched slower than an hour can be emitted twice, and the second FetchFile then fails with not found.
- When the primary node changes, the tar of a lost FlowFile is on the old node's disk. Pull therefore keeps, next to `exported`, a `pending` record of each export (node, tar name, and each image's repository, tag and 16 hex characters of its digest) in cluster state until the node that wrote the tar finds it fetched. A new primary that finds a record of another node older than an hour with no such tar in its own Output Directory exports those images again at the digest the registry has for the tag now (images a later export has moved on, or that are gone, are left out; one that cannot be read from the registry this run waits for the next regular run) and records that export in turn. Nothing here can tell a tar fetched just before the failover from one lost with it, so the worst case is one repeated export, which Push takes as already present.
- One tar per run. With Max Export Size, the images left over go on the next run.
- Pull's FlowFile has no content. Its attributes: `filename`, `Filename`, `absolute.path`, `Content-Type`, `X-Sha256`, `X-Artifact-Type`, `X-Artifact-Format`, `registry.host`, `registry.images`, `file.size`, and the dynamic properties.
- Blobs stream between registry and disk in 8 MiB chunks and are checked against their digests. Memory stays flat whatever the image size. Everything else a registry sends is read with a cap and refused beyond it: 4 MiB for a manifest, 16 MiB for a token, listing or upload answer, 64 MiB for the ledger; an error body is read for its first 300 bytes only.
- Push retries a failed blob upload twice from the tar, authorizing again after a 401. With Delete Tar File it renames the tar while it works, so two tasks given the same path cannot both push it; a tar it cannot delete leaves the FlowFile on `success` with `registry.tar.leftover`.
- Two images of one tar that end at the same repository and tag after Rewrite or Strip Base Path (`a/app:v1` and `b/app:v1` under Rewrite `merged`) fail the tar with nothing pushed, whatever Move Existing Tags says; Pull leaves the second one out of its export and logs why, in every later run too while the first stays in its state at another digest.
- An index may hold other indexes (up to 8 levels). Pull exports every manifest and blob reachable from it, and Push sends each manifest before the index that names it. A deeper graph is skipped by Pull with a warning.
- Every config and layer blob is copied, whatever `urls` its descriptor lists. Only a foreign layer (Docker `rootfs.foreign` or OCI `nondistributable` media type) that has `urls` is left to those urls, as before.
- Push checks every target tag before the first upload, skips blobs the registry holds, mounts a blob shared across repositories, and reads each tag back. Its success FlowFile carries `registry.images`, `registry.pushed` and `registry.present`.
- Registry URL must not contain a user name or password (use Username and Password). Error text, `registry.error` and the log show a URL as scheme, host and path only, so a pre-signed storage signature or embedded credential is never written there.
- Catalog and tag listings follow the `rel=next` link of a `Link` header to the end, whatever order its parameters come in.
- A redirect to object storage is followed without the registry's credentials, and so is an upload Location on another host. Only http and https are followed, an https registry is never sent to plain http, and a host that resolves to a loopback, link-local (cloud metadata), private or other non-public address is refused. A registry whose object storage is on a private address (an on-premises MinIO) needs that host or range in **Trusted Storage Hosts** on Pull and Push, for example `minio.internal, 10.20.0.0/16`. The registry's own host is allowed at every address its name resolved to at the first connection (a registry with several addresses answers on all of them). A later connection to that name (a redirect or upload Location on the same host) at an address the name was not seen to resolve to is sent without the registry's credentials, and only if it passes the same checks as object storage, so a name re-pointed after the first answer gets no credentials.

## Environment variables

Registry URL (Pull and Export), Username, and Pull's and Export's Repositories, Ledger URL, Image List, Rewrite Base Path and Output Directory are evaluated against the NiFi environment, so `${REGISTRY_HOST}` or `https://${REGISTRY_HOST}:5000` read the environment variable of that name. NiFi 2.11's Python API answers a plain `${NAME}` from the attribute map alone, so `nifi_attributes.py` puts the environment behind that map. Push's Registry URL, Tar File and Rewrite Base Path read FlowFile attributes instead.

## Upgrading to 2.0.0

**CA Certificate File was removed** from Pull and Push, so a flow that set it no longer trusts that CA (Pull would carry the old value as a dynamic attribute; delete it) until you set **SSL Context Service** instead: a StandardSSLContextService (or any SSLContextProvider) whose truststore holds the CAs. The processors read the CA certificates from that truststore and trust them in addition to the system store. A keystore in the service is not used (no client certificates). Flows pinned to 1.4.0 keep working until you change the version.

## Preconditions

- Output Directory and the PutFile directory exist on every node and are writable by the NiFi user.
- `SSL_CERT_FILE` names the CA bundle, such as the cert-manager `ca.crt`, or SSL Context Service is set.

## Expected result

```sh
curl -s -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json' \
  https://registry.high.example.internal/v2/team/app/manifests/1.0 | sha256sum
```

The digest matches the one in `registry.images`.
