# container-registry

PlanRegistryTransfer and RouteRegistryTransfer as NiFi Python processors: the compact native layer transfer, plain source with no build. Plan plans the jobs of a gitlab-registry-mirror webhook. Route drives native InvokeHTTP downloads and uploads, one FlowFile per blob and manifest. Image bytes stay in NiFi's content repository: no tar and no staging directory.

1.7.0 holds waiting manifests at Wait and Notify instead of a timer: a 1.6.0 high side needs the new cards ([ADR 0009](../../docs/decisions/adr-0009-notify-wait-manifests.md)). 1.6.0 removed PullContainerImages, ExportContainerImages and PushContainerImages (the tar option O1). Use 1.5.x for them. See [ADR 0008](../../docs/decisions/adr-0008-remove-tar-processors.md).

## More documentation

**The build guide is [docs/runbooks/image-transfer-build-by-hand.md](../../docs/runbooks/image-transfer-build-by-hand.md).** [../../docs/README.md](../../docs/README.md) maps the decision records, the runbooks, the testing checklist, the input contract and the attribute reference.

## Requirements

- Apache NiFi 2.x with Python enabled (`nifi.python.command`), Python 3.10 or later on the NiFi host.
- Nothing from PyPI. `python/requirements.txt` lists no packages and sits outside this directory, so NiFi never runs `uv` for it and `UV_INDEX_URL` is not consulted.
- Content repository space for the blobs in flight. Python never holds a blob, so memory does not grow with image size.

## Install

Copy this whole directory, helper modules included, into the Python extensions directory (`nifi.python.extensions.source.directory.default`, `./python_extensions`) of every node, or onto the share mounted there:

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

NiFi discovers the processors within a minute, listed as `PlanRegistryTransfer 1.7.0` and `RouteRegistryTransfer 1.7.0` (bundle `python-extensions`). An edited file reloads when the processor next starts.

## Properties

Generated from the descriptors (`python3 python/tests/test_readme_tables.py --write`). The unit tests fail when they drift. Plan reads only the source registry, so its properties say `Source Registry`. Route runs on both sides, so its `Registry URL` is the source on the low side and the target on the high side.

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

## Usage

Low side:

```text
ListenHTTP (HTTP Headers for Attributes X-.*)
  -> PlanRegistryTransfer (Source Registry URL, Allowed Repositories team/*)   failure -> Hold
  -> SplitJson ($.jobs)
  -> RouteRegistryTransfer (Transfer Side Low, Registry URL)
       prepared -> InvokeHTTP (HTTP URL ${registry.http.url}) -> back to RouteRegistryTransfer
       unverified -> CryptographicHashContent -> back to RouteRegistryTransfer
       retryable -> RetryFlowFile -> back to RouteRegistryTransfer
       complete -> ToDiode   failure -> Hold
```

High side:

```text
FromDiode
  -> RouteRegistryTransfer (Transfer Side High, Registry URL)
       prepared -> InvokeHTTP (HTTP URL ${registry.http.url}) -> back to RouteRegistryTransfer
       retryable -> RetryFlowFile -> back to RouteRegistryTransfer
       signal -> Notify (signal ${registry.signal.id}) -> back to RouteRegistryTransfer
       waiting -> Wait (signal ${registry.digest}, target ${registry.wait.expected})
                    success -> back to RouteRegistryTransfer
                    expired -> RetryFlowFile (registry.wait.retries) -> back, or after the limit -> LogMessage -> IncompleteImages
       complete -> auto-terminate   failure, unverified -> Hold
```

Hold and IncompleteImages are disabled LogMessages: their incoming queues keep failures and incomplete images for inspection. Notify and Wait share a cache every node reaches (Redis in this estate), [ADR 0009](../../docs/decisions/adr-0009-notify-wait-manifests.md). Both InvokeHTTP cards send Authorization `${registry.auth.header}`. Every card, property, connection and queue setting is in the [build guide](../../docs/runbooks/image-transfer-build-by-hand.md). Instead of a webhook, a FlowFile with the attribute `registry.image.reference` (`team/app:1.0` or `team/app@sha256:...`) plans one image, and `registry.layers` with `registry.check` resends chosen layers: see the [input contract](../../docs/reference/contract.md).

## Behaviour

- Plan accepts a webhook only when `X-Sha256` is the sha256 of the body, the body names the host of Source Registry URL, every repository is in Allowed Repositories, and every digest is what the registry resolves now. Otherwise it fails the FlowFile with `registry.error` and plans nothing. The `X-CI-*`, `X-Image-Count` and `X-Registry` headers are never read.
- Plan reads manifests and HEAD checks only, never blobs. Each job carries repository, kind, digest, size, media type, reference, dependencies, image, platform and a check code. Manifests and indexes carry their exact source bytes, base64 encoded, so digests stay byte-identical.
- Route reads the job JSON once, on the low side's first pass. After that, and on the high side, it never reads FlowFile content: every callback returns `contents=None`. Content above 64 MiB on that first pass is refused.
- Route handles only metadata HTTP: token discovery, HEAD checks and starting an upload. Native InvokeHTTP streams the bytes. Uploads are an empty POST then a native PUT. A manifest is first PUT by digest, and only after the registry confirms that digest is the tag PUT.
- On the high side a manifest waits at a native Wait until every blob or child manifest it needs is present. Each stored blob or manifest signals the manifests that need it through Notify, so a manifest moves on as soon as its last dependency is in. The registry stays the authority: Route checks it again on every release and every Wait expiry. Each check writes `registry.wait.received`, `registry.wait.expected`, `registry.wait.missing` and `registry.manifest.digest`. A RetryFlowFile bounds the wait; after it the manifest goes to a fail queue with a report of what is missing.
- Network errors, 401, 408, 429 and 5xx go to `retryable` with a limit of three retries. A digest or size that differs from the plan goes to `failure`.
- Source authentication is cleared after the download, before the diode, and on every retry, failure and completion path. This stops the token from being forwarded. It does not erase earlier provenance.
- Payload redirects are refused. Basic credentials are never copied into attributes: for a Basic-auth registry set them on the native InvokeHTTP's Request Username and Request Password.
- Everything a registry sends is read with a cap: 4 MiB for a manifest, 16 MiB for a token or upload answer. A URL in error text and the log shows scheme, host and path only.

## Environment variables

Source Registry URL, Source Registry Username, Allowed Repositories and Platform Selection on Plan, and Registry URL, Registry Username and Target Repository on Route, are evaluated against the NiFi environment, so `${REGISTRY_HOST}` or `https://${REGISTRY_HOST}:5000` read the environment variable of that name. NiFi 2.11's Python API answers a plain `${NAME}` from the attribute map alone, so `nifi_attributes.py` puts the environment behind that map.

## Preconditions

- The high-side account may create repositories, or they exist.
- `SSL_CERT_FILE` names the CA bundle, or SSL Context Service is set, for the Python requests. Native InvokeHTTP needs its own SSL Context Service.
- Queue size thresholds exceed the largest single blob.

## Expected result

```sh
skopeo inspect --raw docker://registry.high.example.internal/team/app:1.0 | sha256sum
```

The digest matches the one in the webhook body.
