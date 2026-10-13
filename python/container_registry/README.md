# container_registry

PushContainerImages and PullContainerImages as NiFi Python processors: same tar, same attributes as the NAR, plain source.

## Requirements

- Apache NiFi 2.x with Python enabled (`nifi.python.command`), Python 3.10 or later on the NiFi host.
- Nothing from PyPI. `requirements.txt` lists no packages, so there is nothing to mirror across the diode and `UV_INDEX_URL` is not consulted.
- Disk on every node for the largest export, twice: the tar Pull writes, and the tar Push reads.

## Install

Copy this directory into the Python extensions directory (`nifi.python.extensions.source.directory.default`, `./python_extensions`) of every node, or onto the share mounted there:

```sh
cp -r python/container_registry /opt/nifi/nifi-current/python_extensions/
```

NiFi discovers both processors within a minute, listed as `PullContainerImages 1.3.0` and `PushContainerImages 1.3.0` (bundle `python-extensions`). An edited file reloads when the processor next starts.

## Properties

The full set, with descriptions, is in `PullContainerImages.py` and `PushContainerImages.py` and on each processor's Usage page.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | Registry URL | none | `https://host`, `http://host:5000`, or Artifactory's `https://host/artifactory/api/docker/<repo-key>` |
| Optional | Username, Password | none | Empty for an open registry |
| Optional | CA Certificate File | none | Extra PEM CAs. The system store and `SSL_CERT_FILE` are always trusted |
| Optional | Rewrite Base Path, Strip Base Path | none, `false` | Replace or remove the first path segment of every repository |
| Pull | Image Source | `Registry Listing` | `Registry Listing` (**Repositories**, `team/*`, `*`), `Ledger` (**Ledger URL**), or `Image List` (**Image List**, one image per line) |
| Pull | Output Directory | none | Where each tar is written; FetchFile reads it from there |
| Pull | Initial Run | `Export All` | `Record Only` baselines the state and exports only later changes |
| Pull | Max Export Size | no limit | Blob bytes per tar. What does not fit goes on the next run |
| Pull | *(dynamic)* | none | Becomes an attribute on each FlowFile, Expression Language over its attributes |
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

A FlowFile out of Push still holds the tar. Auto-terminate `success`, or give its queue a back-pressure size above the largest tar; a full queue stops Push.

## Behaviour

- Pull keeps `repository:tag -> digest` in cluster state, pruned to what the source still lists, and writes it only after the tar is complete. The tar is written as a hidden `.<name>.partial` and renamed when done. A failed run leaves no tar and no state change.
- One tar per run. With Max Export Size, the images left over go on the next run.
- Pull's FlowFile has no content. Its attributes: `filename`, `Filename`, `absolute.path`, `Content-Type`, `X-Sha256`, `X-Artifact-Type`, `X-Artifact-Format`, `registry.host`, `registry.images`, `file.size`, and the dynamic properties.
- Blobs stream between registry and disk in 8 MiB chunks and are checked against their digests. Memory stays flat whatever the image size.
- Push checks every target tag before the first upload, skips blobs the registry holds, mounts a blob shared across repositories, and reads each tag back. Its success FlowFile carries `registry.images`, `registry.pushed` and `registry.present`.
- A redirect to object storage is followed without the registry's credentials.

## Preconditions

- Output Directory and the PutFile directory exist on every node and are writable by the NiFi user.
- `SSL_CERT_FILE` names the CA bundle, such as the cert-manager `ca.crt`, or CA Certificate File is set.

## Expected result

```sh
curl -s -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json' \
  https://registry.high.example.internal/v2/team/app/manifests/1.0 | sha256sum
```

The digest matches the one in `registry.images`.
