# Building the webhook flow by hand in the NiFi 2.x UI

To build the transfer across a data diode, follow [the image transfer build guide](image-transfer-build-by-hand.md). This page describes the separate single-NiFi flow in which ExportContainerImages writes a tar and PushContainerImages pushes it.


The flow: gitlab-registry-mirror posts a webhook, ExportContainerImages writes the named images to a tar on local disk, PushContainerImages pushes that tar into the target registry.

```text
ListenHTTP -> RouteOnAttribute -> ExportContainerImages -> PushContainerImages
```

Hosts in this page are placeholders: the source registry is `registry.example.com`, the target is `registry-high.example.com`, NiFi is `nifi.example.com`. Do the install, truststore and parameter steps of [QUICKSTART.md](../../QUICKSTART.md) first (sections 1 to 3); this page starts at the canvas. The property tables in [README.md](../../README.md) list every property and default.

## 1. Process group and parameters

1. On the canvas, drag the **Process Group** icon in the top toolbar onto the canvas. Name it `registry-webhook`, click **Add**.
2. Right-click the group > **Configure**. Under **Process Group Parameter Context** choose `registry` (created in the quickstart), click **Apply**.
3. Double-click the group to enter it. The breadcrumb at the bottom left should read `NiFi Flow > registry-webhook`.

## 2. ListenHTTP

1. Drag the **Processor** icon onto the canvas, type `ListenHTTP` in the filter, select it, **Add**.
2. Right-click it > **Configure** > **Properties**. Set:

| Property | Value |
|---|---|
| Base Path | `contentListener` (default) |
| Listening Port | `8081` |
| HTTP Headers for Attributes | `X-.*` |
| Return Code | `200` (default) |

   Leave **SSL Context Service** empty for plain HTTP on a trusted network. To use TLS, choose the StandardSSLContextService here, but it must then hold a keystore as well as the truststore.
3. **Relationships** tab: `success` is the only one, you connect it below. **Scheduling** tab: leave the defaults (Timer driven, 0 sec). **Apply**.

The header regex matters: without it `X-Sha256` and `X-Artifact-Type` are dropped and every webhook fails.

## 3. RouteOnAttribute

1. Add a **RouteOnAttribute** processor below ListenHTTP.
2. Configure > **Properties**. Set **Routing Strategy** to `Route to Property name`. Click **+** (top right of the property list), name the property `request`, value:

```text
${X-Artifact-Type:equals('container-images-request')}
```

3. **Relationships** tab: tick **terminate** for `unmatched`. Leave `request` unticked, you connect it below. **Apply**.

## 4. ExportContainerImages

1. Add an **ExportContainerImages** processor below RouteOnAttribute. If it is not listed, the extension is not installed on this node: see the quickstart, section 1.
2. Configure > **Properties**:

| Property | Value |
|---|---|
| Source Registry URL | `https://registry.example.com` |
| Source Registry Username | `#{source_registry_user}` |
| Source Registry Password | `#{source_registry_password}` |
| SSL Context Service | the StandardSSLContextService (omit for a public CA) |
| Trusted Storage Hosts | empty, or the on-premises storage the registry redirects to, for example `minio.example.com` |
| Allowed Repositories | `team/*` |
| Tar Output Directory | `#{container-images.tar.dir}` (a parameter in the `registry` context with value `/data/out`; see the quickstart) |
| Tar Filename Prefix | `container-images-` (default) |
| X-Artifact-Type Value | `container-images` (default) |
| Max Export Size | empty (no limit) |
| Tar Per Image | `false` (default; `true` writes one tar per image, repeats shared layers and sends more bytes over the diode) |
| Request Timeout | `600` (default) |

   Allowed Repositories is an allowlist: the webhook body names the images, and any image outside it fails the whole FlowFile.
3. **Relationships** tab: `original` is already terminated. Leave `success` and `failure` unticked, you connect both below. **Scheduling**: leave the defaults. **Apply**.

## 5. PushContainerImages

1. Add a **PushContainerImages** processor below ExportContainerImages.
2. Configure > **Properties**:

| Property | Value |
|---|---|
| Target Registry URL | `https://registry-high.example.com` |
| Target Registry Username | `#{target_registry_user}` |
| Target Registry Password | `#{target_registry_password}` |
| SSL Context Service | the StandardSSLContextService (omit for a public CA) |
| Trusted Storage Hosts | empty, or the target's private storage host |
| Input Tar Path | `#{container-images.tar.dir}/${filename}` |
| Delete Input Tar | `true` |
| Rewrite Target Base Path | empty (images keep their repository name) |
| Strip Target Base Path | `false` (default) |
| Move Existing Tags | `true` (default; a tag at another digest moves, as docker push does. `false` fails the tar instead) |
| Request Timeout | `600` (default) |

   Set Rewrite and Strip together and the push fails: use at most one.
3. **Relationships** tab: tick **terminate** for `success`. `original` is already terminated. Leave `failure` unticked. **Apply**.

Input Tar Path is read from the local disk of the node that runs Push, and Export wrote it on its own node. On a cluster the FlowFile stays on the node that ran Export as long as the Export to Push connection has **Load Balance Strategy** `Do not load balance` (the default), so Push finds the tar. Keep it that way, or mount shared storage at that path on every node. Put it on a dedicated volume, never inside NiFi's content, FlowFile or provenance repository directories (README, "Where to put the tar directory"). NiFi's Python API passes FlowFile content as one in-memory block, so the tar is not moved through the FlowFile.

Headers such as `X-CI-Project`, `X-CI-Pipeline-Url`, `X-CI-Commit-Sha`, `X-Image-Count` and `X-Registry` are kept as attributes by the `X-.*` regex in step 2, so you can see them in provenance. Export ignores them: it trusts only the body, `X-Sha256` and the registry's digest. On the success FlowFile it adds `registry.image.count` and `registry.image.N.repository`, `.tag` and `.digest`.

## 6. Connect them

Hover over a processor until the blue arrow appears, drag it onto the next processor, pick the relationship in the dialog, click **Add**.

| From | To | Relationship |
|---|---|---|
| ListenHTTP | RouteOnAttribute | success |
| RouteOnAttribute | ExportContainerImages | request |
| ExportContainerImages | PushContainerImages | success |
| ExportContainerImages | a LogAttribute (or a funnel) | failure |
| PushContainerImages | the same LogAttribute (or funnel) | failure |

A LogAttribute with every relationship terminated writes the attributes, `registry.error` included, to `nifi-app.log`. A funnel with nothing after it instead holds the FlowFile in its queue, so you can inspect it in the UI.

## 7. Start and check validity

1. Click an empty part of the canvas, then **Start** in the Operate palette. Every processor should show a green play icon.
2. A red warning icon on a processor means it is invalid: hover it to read why. The usual causes are an unterminated relationship, a parameter context not bound to the group, or a typo in a property name.

## 8. Send a test webhook

Follow QUICKSTART section 5 to compute the digest and `X-Sha256` and post with curl. A `200` means ListenHTTP took it.

## 9. Check provenance

1. Right-click PushContainerImages > **View data provenance** (or the menu at the top right > **Data Provenance**, then filter by processor).
2. Open the newest event with the **i** icon. **Attributes** tab: `registry.pushed` counts the tags pushed, `registry.present` the tags already at their digest, `registry.images` lists them.
3. To follow one FlowFile from the webhook to the push, open the event and click **Lineage** (the graph icon). The path runs ListenHTTP, RouteOnAttribute, ExportContainerImages, PushContainerImages.
4. A refused webhook shows instead as a FlowFile in the `failure` queue of Export (right-click the connection > **List queue**, open a row, **Attributes**, `registry.error`). `X-Sha256 is missing or is not the sha256 of the body` means the posted bytes differ from the hashed bytes.
5. Confirm on the target: `skopeo inspect --raw docker://registry-high.example.com/team/app:1.0 | sha256sum` equals the digest in the webhook.

## Optional: one tar and one FlowFile per image

1. On Export set **Tar Per Image** to `true`. Its success FlowFile then holds one JSON line per tar.
2. Add **SplitText** between Export and Push: Line Split Count `1`, Header Line Count `0`; connect `splits` onward and terminate `original` and `failure`.
3. Add **EvaluateJsonPath**: Destination `flowfile-attribute`, and these properties: `filename` = `$['filename']`, `X-Sha256` = `$['X-Sha256']`, `registry.repository` = `$['registry.repository']`, `registry.tag` = `$['registry.tag']`, `registry.digest` = `$['registry.digest']`. Connect `matched` to Push and terminate `unmatched` and `failure`.
4. Push is unchanged: Input Tar Path `/data/out/${filename}`. Each FlowFile pushes one image.
