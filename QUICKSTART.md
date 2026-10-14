# Quickstart

This gets one container image from a source registry into a target registry through NiFi 2.x, using the Python processors and the gitlab-registry-mirror webhook. All hosts and passwords below are placeholders. For every property and the other flow (scheduled Pull across a diode), see [README.md](README.md). For the same flow built click by click in the UI, see [docs/runbooks/nifi-by-hand.md](docs/runbooks/nifi-by-hand.md).

You need: NiFi 2.x with Python enabled (`nifi.python.command` set), shell access to the NiFi node, `keytool` (any JDK), and an image in the source registry.

## 1. Install the extensions

On every NiFi node:

```sh
git clone https://github.com/djlongy/nifi-container-registry-processors.git
cp -r nifi-container-registry-processors/python/container_registry /opt/nifi/nifi-current/python_extensions/
```

Use the directory `nifi.python.extensions.source.directory.default` names in `nifi.properties` if it is not `./python_extensions`. Within a minute `PullContainerImages`, `ExportContainerImages` and `PushContainerImages` 1.5.0 appear in **Add Processor** (filter on `container`). Nothing is installed from PyPI.

## 2. Trust the registry CA (SSL Context Service)

Skip this when the registries use certificates from a public CA. StandardSSLContextService reads a PKCS12 or JKS truststore; it does not accept a PEM file, so convert the CA first:

```sh
keytool -importcert -noprompt -alias registry-ca -file ca.crt \
  -keystore /opt/nifi/nifi-current/conf/registry-truststore.p12 -storetype PKCS12 -storepass changeit
```

In NiFi: **Controller Settings > Controller Services > +**, add **StandardSSLContextService** with:

| Property | Value |
|---|---|
| Truststore Filename | `/opt/nifi/nifi-current/conf/registry-truststore.p12` |
| Truststore Password | `changeit` |
| Truststore Type | `PKCS12` |

Enable it. The processors trust these CAs in addition to the system store.

## 3. Parameter context with the passwords

**Parameter Contexts > +**, name it `registry`, and add:

| Parameter | Sensitive | Value |
|---|---|---|
| `source_registry_user` | No | `svc-mirror-read` |
| `source_registry_password` | Yes | the source registry token |
| `target_registry_user` | No | `svc-mirror-write` |
| `target_registry_password` | Yes | the target registry token |
| `container-images.tar.dir` | No | `/data/out` |

The last one is the directory Export writes tars to and Push reads them from. A parameter means you can change it later without restarting NiFi (NiFi stops and restarts the processors that use it). Use a dedicated per-node volume at the same path on every node, never inside NiFi's content, FlowFile or provenance repository directories; see [README.md](README.md#where-to-put-the-tar-directory).

Bind it to the process group you build in (right-click the group > **Configure > Parameter Context**).

## 4. Build the flow

Add a process group and these processors, in this order, connecting each relationship named below:

| # | Processor | Properties | Relationship to the next |
|---|---|---|---|
| 1 | ListenHTTP | Listening Port `8081`, Base Path `contentListener`, HTTP Headers to receive as Attributes (Regex) `X-.*` | success |
| 2 | RouteOnAttribute | Routing Strategy `Route to Property name`, new property `request` = `${X-Artifact-Type:equals('container-images-request')}` | request. Auto-terminate `unmatched` |
| 3 | ExportContainerImages | Source Registry URL `https://registry.example.com`, Source Registry Username `#{source_registry_user}`, Source Registry Password `#{source_registry_password}`, SSL Context Service the one from step 2, Allowed Repositories `team/*`, Tar Output Directory `#{container-images.tar.dir}` | success. failure to a LogAttribute or a funnel |
| 4 | PushContainerImages | Target Registry URL `https://registry-high.example.com`, Target Registry Username `#{target_registry_user}`, Target Registry Password `#{target_registry_password}`, SSL Context Service the same, Input Tar Path `#{container-images.tar.dir}/${filename}`, Delete Input Tar `true` | success: auto-terminate. failure to a LogAttribute or a funnel |

The directory (`/data/out`) must exist and be writable by the NiFi user. Export writes the tar there and Push reads it by path, so both processors must run on the same node, or the directory must be shared storage mounted at the same path on every node.

Start all processors. They are valid when none shows a red warning icon.

## 5. Send one webhook

Pick an image and get its digest from the source registry:

```sh
DIGEST=$(curl -sI -u "$USER:$TOKEN" \
  -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
  https://registry.example.com/v2/team/app/manifests/1.0 | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}')
echo "$DIGEST"
```

Write the body to a file, hash that exact file, and post it unchanged (`--data-binary` sends the bytes as they are):

```sh
cat > body.json <<JSON
{"registry": "registry.example.com", "images": [{"repository": "team/app", "tag": "1.0", "digest": "$DIGEST"}]}
JSON
SHA=$(sha256sum body.json | cut -d' ' -f1)    # macOS: shasum -a 256 body.json | cut -d' ' -f1
curl -i -X POST http://nifi.example.com:8081/contentListener \
  -H 'X-Artifact-Type: container-images-request' \
  -H "X-Sha256: $SHA" \
  --data-binary @body.json
```

The sender may add `X-CI-Project`, `X-CI-Pipeline-Url`, `X-CI-Commit-Sha`, `X-Image-Count` and `X-Registry` headers. They stay on the FlowFile as attributes for routing and display and are never used to select or check images: only the body, `X-Sha256` and the registry's own digest count. Export also adds `registry.image.count` and `registry.image.N.repository`, `.tag` and `.digest` (N from 0).

`HTTP/1.1 200 OK` means ListenHTTP accepted it, not that the push worked.

## 6. Check it

- In NiFi, the Export and Push processors show one FlowFile in and out in their stats. Right-click Push > **View data provenance**, open the newest event and read its attributes: `registry.pushed` is `1`, or `registry.present` is `1` when the image was already there.
- On the target:

```sh
skopeo inspect --raw docker://registry-high.example.com/team/app:1.0 | sha256sum
```

The digest matches `$DIGEST`.

If a FlowFile sits in a `failure` queue, **List queue > View the attributes**, and read `registry.error`. The common causes: `X-Sha256 is missing or is not the sha256 of the body` (the body was edited after hashing, or a trailing newline differs), `... is not in Allowed Repositories`, and `the body names registry X, not Y` (the body's `registry` must be the host of Source Registry URL).

To get one tar and one FlowFile per image, set Tar Per Image to `true` on Export and add SplitText and EvaluateJsonPath between Export and Push: see the README, "One tar per image". The default `false` writes one tar for all images and stores shared layers once.
