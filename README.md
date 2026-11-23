# nifi-container-registry-processors

NiFi Python processors that move container images from one registry to another, for example across a data diode, digests unchanged, with no skopeo or docker on the NiFi host. PlanRegistryTransfer plans the transfer from a gitlab-registry-mirror webhook. RouteRegistryTransfer drives native InvokeHTTP downloads and uploads. Each blob and each manifest is its own FlowFile, and image bytes live only in NiFi's content repository: no tar and no staging directory.

The Python processors in `python/container-registry/` are the maintained implementation. They need no build and no dependencies. The earlier NAR releases (v1.x on GitHub) are no longer maintained.

1.7.0 holds waiting manifests at a native Wait released by Notify, with a bounded retry and a fail queue ([ADR 0009](docs/decisions/adr-0009-notify-wait-manifests.md)). 1.6.0 removed PullContainerImages, ExportContainerImages and PushContainerImages (the tar option O1). Flows that use them stay on 1.5.x. See [ADR 0008](docs/decisions/adr-0008-remove-tar-processors.md).

## More documentation

**To build the transfer, follow one guide: [the image transfer build guide](docs/runbooks/image-transfer-build-by-hand.md).** It is split into a low-side and a high-side part, with canvas screenshots, and each side lists its own properties changed from default, connections and recovery steps. [QUICKSTART.md](QUICKSTART.md) is the shortest path to one image. Only PlanRegistryTransfer and RouteRegistryTransfer are maintained. Designs the project did not adopt were removed on 2026-11-23 and remain in git history.

For what each processor requires of its input (body fields, attributes and headers, and the error text and relationship when they are missing or wrong), see the [input contract](docs/reference/contract.md).

[docs/README.md](docs/README.md) maps the decision records, the runbooks, the testing checklist and the attribute reference.

## Requirements

- Apache NiFi 2.x with Python enabled (`nifi.python.command`), Python 3.10 or later on the node. The processors use the Python standard library only.
- NiFi's standard processors: ListenHTTP, SplitJson, InvokeHTTP, CryptographicHashContent, RetryFlowFile.
- Any registry that follows the distribution API: `registry:2` (open, htpasswd or token auth), Quay, Artifactory, Harbor, GitLab.
- Content repository space for the blobs in flight. Queue size thresholds must exceed the largest single blob.

## Install

Copy the whole `python/container-registry` directory, helper modules included, into the Python extensions directory (`nifi.python.extensions.source.directory.default`, `./python_extensions`) of every node, or onto the share mounted there:

```sh
cp -r python/container-registry /opt/nifi/nifi-current/python_extensions/
```

On Kubernetes, build `python/Containerfile` and copy from it in an init container, so every pod has the processors before NiFi starts:

```yaml
initContainers:
  - name: python-extensions
    image: <registry>/<ns>/nifi-container-registry-python@sha256:<digest>
    command: ["sh", "-c", "rm -rf /target/container-registry && cp -r /extensions/container-registry /target/"]
    volumeMounts:
      - name: python-extensions
        mountPath: /target
```

NiFi discovers the processors within a minute, listed as `PlanRegistryTransfer 1.7.0` and `RouteRegistryTransfer 1.7.0` (bundle `python-extensions`).

## Python processors 1.7.0

| Processor | Side | What it does |
|---|---|---|
| PlanRegistryTransfer | low | Checks the webhook (or a `registry.image.reference` attribute) against Allowed Repositories, resolves each image to immutable digests, and emits one JSON job list (configs, layers, manifests, indexes) for SplitJson. Reads manifests only, never blobs |
| RouteRegistryTransfer | both | Prepares each registry request for native InvokeHTTP, checks digests, sizes and dependencies, and routes downloads, uploads, retries, waits and failures. Never reads blob content |

The low side needs both processors, the high side RouteRegistryTransfer only. The flow, in short:

```text
low:  ListenHTTP -> PlanRegistryTransfer -> SplitJson -> RouteRegistryTransfer <-> InvokeHTTP (GET), CryptographicHashContent, RetryFlowFile
        complete -> the diode
high: the diode -> RouteRegistryTransfer <-> InvokeHTTP (PUT), RetryFlowFile
        complete -> auto-terminate
```

Both InvokeHTTP cards take every request from attributes RouteRegistryTransfer writes per FlowFile: HTTP URL is `${registry.http.url}` and Authorization is `${registry.auth.header}`. Never put the registry address in HTTP URL: each blob and manifest has its own URL. The [build guide](docs/runbooks/image-transfer-build-by-hand.md) has every card, property and connection. [ADR 0007](docs/decisions/adr-0007-compact-native-layer-transfer.md) records the design.

A webhook whose `X-Sha256` does not match its body, or whose registry host, repositories or digests do not check out, fails with `registry.error` and plans nothing. The `X-CI-*`, `X-Image-Count` and `X-Registry` headers are for routing and display only ([ADR 0005](docs/decisions/adr-0005-webhook-trust.md)).

### Properties

Generated from the descriptors. `python3 python/tests/test_readme_tables.py --write` rewrites them, and the unit tests fail when they drift.

**PlanRegistryTransfer**

<!-- properties:PlanRegistryTransfer -->
| Name | Required | Default | Description |
|---|---|---|---|
| Source Registry URL | Yes | none | Registry to read manifests from, such as https://registry.example.com, or https://host/artifactory/api/docker/repo-key for Artifactory. No user:password in the URL. Required, no default. Environment variables allowed: ${REGISTRY_URL}. |
| Source Registry Username | No | none | User for the source registry. Empty (default): anonymous access, which works only on a registry that allows it. Environment variables allowed: ${REGISTRY_USER}. |
| Source Registry Password | No | none | Password or access token for the source registry. Empty (default): none sent. Sensitive: reference a parameter, such as #{gitlab.container.token}. |
| SSL Context Service | No | none | A StandardSSLContextService whose PKCS12 or JKS truststore holds the registry's CA (it does not accept PEM). Its CAs add to the system store and SSL_CERT_FILE. Empty (default): only those are trusted. Its keystore is ignored. |
| Allowed Repositories | Yes | none | Allowlist, not the selection: the FlowFile picks the images and each must be in here. Comma or new line separated: team/app, or team/* for a prefix, * for all. Required. Any other repository fails the FlowFile. |
| Platform Selection | Yes | `all` | all (default): every platform, index kept, so its digest is the source's. Or platforms such as linux/amd64, linux/arm64/v8 (comma or new line separated): only those are planned, without the index; a single match gets the tag, on its own digest. |
| Maximum Manifest Depth | Yes | `16` | Most indexes nested in one another before an image is refused as malformed. Default 16. |
| Maximum Jobs Per Transfer | Yes | `1000` | Most jobs (configs, layers, manifests and indexes, a blob shared by platforms counted once) one image may plan. More fails the FlowFile. Default 1000. |
| Request Timeout | Yes | `60` | Seconds a registry read may stall before it fails. Default 60. Only manifests and HEAD checks are read here, never blobs. |
<!-- /properties -->

**RouteRegistryTransfer**

<!-- properties:RouteRegistryTransfer -->
| Name | Required | Default | Description |
|---|---|---|---|
| Transfer Side | Yes | `Low` | Low prepares downloads; High prepares uploads. |
| Registry URL | Yes | none | Registry origin or API base URL; never comes from a webhook. |
| Registry Username | No | none | Registry user for metadata and token requests. |
| Registry Password | No | none | Registry password or robot token, kept in a sensitive parameter. |
| SSL Context Service | No | none | Truststore CAs added to the Python system trust store. |
| Target Repository | No | none | High-side repository override; empty keeps the source repository. |
| Request Timeout | Yes | `60` | Metadata request timeout in seconds; native payload timeouts are separate. |
<!-- /properties -->

## Changing the processors

The processors are plain Python so that a person, or a local model, can read, change and test them without compiling anything. Follow these steps in order.

### Layout (`python/container-registry/`)

| File | Holds |
|---|---|
| `PlanRegistryTransfer.py` | PlanRegistryTransfer: checks the request, reads manifests, writes the job list |
| `RouteRegistryTransfer.py` | RouteRegistryTransfer: prepares registry requests and routes the native callbacks |
| `container_registry_client.py` | The registry client shared by the two: authentication, manifest reads, blob HEAD checks, upload starts, the webhook body check |
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

1. Copy the changed files over the ones in the Python extensions directory of the node (`cp -r python/container-registry <python extensions directory>/`).
2. Stop the processor and start it again. Per NiFi's Python Developer Guide ("Processor Reloading"), a changed source takes effect when the processor is next started, with no NiFi restart. Stopped processors may also reload on validation.
3. If the processor never loaded successfully before, NiFi may not watch it for changes: restart NiFi.
4. When behaviour changes, raise `version` in the processor's `ProcessorDetails` (and the same number in this README and `python/container-registry/README.md`), so a flow can tell the versions apart. NiFi can hold two versions of one processor side by side. Choose one with **Change Version** on the processor.

## Preconditions

- The high-side account may create repositories, or they exist. Quay: a robot with write on the repository or creator on the organization.
- Artifactory: use the per-repository API base `https://<host>/artifactory/api/docker/<repo-key>` as Source Registry URL or Registry URL.
- The registries are reachable from every NiFi node of their side. Native InvokeHTTP needs its own trust configuration for a private CA.

## Out of scope

- Signing or verifying signatures, and scanning.
- Deleting tags or repositories.
- Carrying the diode itself: the low side ends at a `ToDiode` port, and the diode sender is supplied by the diode operator.

## Expected result

RouteRegistryTransfer on the high side routes each job to `complete`, and every queue drains. Check a tag on the far side:

```sh
skopeo inspect --raw docker://registry.high.example.internal/team/app:1.0 | sha256sum
```

The digest matches the one in the webhook body.
