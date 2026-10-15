# Building the native layer flow by hand: Python plans, NiFi moves every byte

One Python processor, `PlanRegistryTransfer`, reads manifests and emits a list of jobs. Everything that touches layer bytes is a native NiFi processor (`InvokeHTTP`), so no layer is ever held by Python and each layer is its own FlowFile. Digests and tags are the source's, because manifests travel as the exact source bytes. Choice and evidence: [ADR 0006](../decisions/adr-0006-moving-large-images-through-nifi.md). This page replaces the hand-built parts of [nifi-by-hand-o2-layer-flow.md](nifi-by-hand-o2-layer-flow.md) with the planner; the Python-free variant there still works.

Two ready-made definitions import both groups: [native-layer-flow-low.json](../flows/native-layer-flow-low.json) and [native-layer-flow-high.json](../flows/native-layer-flow-high.json). Hosts are placeholders: source `registry.example.com`, target `registry-high.example.com`. The low group has 21 processors and an output port, the high group 25 and an input port. Build it by hand below to learn it, or import it (section 6) and change the parameters.

```text
LOW  request -> plan -> split jobs -> job to attributes -> source settings -> route by kind
       blob     -> token check -> needs token -> [mint token -> bearer header] -> get blob -> drop token -> hash content
                   (get blob 5xx or 401 -> retry get -> token check)
       manifest -> manifest body -> decode manifest ----------------------------------------> hash content
       hash content -> verify digest and size -> to diode (output port)

HIGH from diode -> target settings -> target repository -> route by kind
       blob     -> token check -> needs token -> [mint token -> bearer header] -> head blob -> exists or missing
                   exists: skip existing
                   missing: start upload -> upload url -> upload url ok -> put blob -> blob done
       manifest -> manifest gate -> gate status -> done: manifest done
                                                   incomplete: incomplete bulletin -> retry gate -> manifest gate
       everything that fails -> scrub (deletes the token) -> hold (a disabled LogMessage, so the FlowFile and its content stay)
```

## 0. What you need

- NiFi 2.x with `python/container-registry/` in the Python extensions directory of every node (restart once so NiFi finds `PlanRegistryTransfer`). Without the planner the low side still works from a job list: replace `request` and `plan` with a `GenerateFlowFile` whose Custom Text is a job list.
- On every high-side node, `python/scripts/manifest_gate.py` and `push_from_stdin.py` side by side, for example in `/opt/nifi/scripts/`. Python 3 is in the NiFi image.
- Any registry that follows the OCI distribution spec. The default upload is the portable two-request form (section 3.5), proven on `registry:2` (distribution 2.8.3), zot and Quay. zot needs `"compat": ["docker2s2"]` under `http` to accept Docker schema 2 manifests. A single-request upload exists as an optional shortcut (section 8).
- Manifest requests must send the full Accept list: OCI index, OCI manifest, Docker manifest list and Docker manifest v2. With only the Docker v2 type, `registry:2` converts an OCI manifest and returns different bytes, so the digest changes. The planner and `manifest_gate.py` already send all four; do the same in any `InvokeHTTP` you add that reads a manifest.
- An SSL Context Service with the registries' CAs if they use a private CA (section 5).

## 1. Parameters

Create the parameter context `registry-native-layer` and bind it to both groups (Process Group > Configure > Process Group Parameter Context).

| Parameter | Sensitive | Value |
|---|---|---|
| `src_url` | no | `https://registry.example.com` (no trailing slash) |
| `src_user` | no | account that may pull the images |
| `src_password` | yes | its password or token |
| `src_token_url` | no | `https://registry.example.com/v2/auth?service=registry.example.com` for a registry with a token endpoint (Quay, Docker Hub style). Empty (default): the registry takes the credentials directly, or is open, and no token is minted |
| `allowed_repositories` | no | `team/*` (comma or new line separated; `*` for all) |
| `platforms` | no | `all` (default), or `linux/amd64, linux/arm64/v8` |
| `dst_url` | no | `https://registry-high.example.com` |
| `dst_user` | no | account that may push |
| `dst_password` | yes | its password or token |
| `dst_token_url` | no | as `src_token_url`, for the target |
| `dst_repository` | no | empty (default): the target keeps the source repository name. A name such as `team/mirror`: every image goes to that repository |
| `gate_script` | no | `/opt/nifi/scripts/manifest_gate.py` |

A registry with a token endpoint refuses basic auth on `/v2/` and wants a Bearer token from `GET /v2/auth?service=<host>&scope=repository:<repo>:pull[,push]` with basic credentials. A `POST` to that endpoint answers 405, so NiFi's OAuth2 token provider cannot be used: `InvokeHTTP` mints the token (sections 2.7 and 3.2).

Whether the mint stage is needed depends on the registry. Leave `src_token_url` or `dst_token_url` empty to skip it: the route sends the FlowFile straight on and no `Authorization` header is sent by the mint.

| Registry | Auth mode | Mint stage needed |
|---|---|---|
| Quay | Bearer token from `/v2/auth`, valid 3600 s, no permanent credential on `/v2` (Basic, a robot token as Bearer and an OAuth token all return 401) | Yes. Proven on Quay 3.15.7 |
| `registry:2` with htpasswd | HTTP Basic: set Request Username and Request Password on the `InvokeHTTP` processors and on the gate (`REGISTRY_USER`, `REGISTRY_PASSWORD`) | No (not run here) |
| `registry:2` open, zot | None | No. Proven in the lab |
| Artifactory | A static Bearer access token: put it in `auth.header` (`Bearer <token>`, from a sensitive parameter in `source settings` and `target settings`) | No (not run here) |

## 2. Low side (group `native layer flow low`)

Use one concurrent task on every processor (the default). Every connection to `hold` below is the failure path; do not auto-terminate it.

1. **GenerateFlowFile** `request`: File Size `0B`, Scheduling `Timer driven`, run once from the context menu. Add two dynamic properties (they become attributes): `registry.image.reference` = `team/app:1.0` (repository and tag, or `team/app@sha256:...`) and `transfer.id` = any correlation id. A webhook replaces it: a `ListenHTTP` whose body is the gitlab-registry-mirror request, with the planner reading the body ([nifi-by-hand.md](nifi-by-hand.md) section 2).

   **A GenerateFlowFile starts with none of the webhook's attributes.** The real webhook sends its facts as HTTP headers and ListenHTTP turns them into attributes; this card has only `mime.type`, `filename`, `path` and `uuid` plus what you add. With `registry.image.reference` set, the planner reads only these attributes, so add exactly these, one dynamic property each: `registry.image.reference` (required, `team/app:1.0`, read by `plan`), `transfer.id` (optional, any correlation id, read by `plan` and copied to every job), and for a layers-only resend `registry.layers` (`2,3`), `registry.check` (the bulletin's 8 characters, required with `registry.layers`) and `registry.platform` (optional). `plan` reads no `X-*` header in this mode. If you put a RouteOnAttribute in front of it, add the attribute it tests. To stand in for the webhook body itself instead (Custom Text), add `X-Sha256` (and `X-Artifact-Type` when a route tests it) and follow [the input contract](../reference/contract.md#standing-in-for-the-webhook-with-generateflowfile) for the byte-identical body rule.
2. **PlanRegistryTransfer** `plan`: Source Registry URL `#{src_url}`, Source Registry Username `#{src_user}`, Source Registry Password `#{src_password}`, Allowed Repositories `#{allowed_repositories}`, Platform Selection `#{platforms}`, SSL Context Service if needed, other properties at their defaults. `success` to `split jobs`, `failure` to `hold` (the attribute `registry.error` says why). It emits one FlowFile, `{"transfer.id": ..., "jobs": [...]}`, and reads manifests and `HEAD`s every blob only. Each job has `kind` (`config`, `blob`, `manifest`, `index`), `repository`, `digest`, `size`, `mediaType`, `tag` (the top manifest only), `reference` (the tag, else the digest), `layer` and `layers` (layer N of M, 0 for the config; a blob that sits at more than one position of the image is one job, with the other positions in `also`), `platform`, `check` (the first 8 hex characters of the top digest), `needs` (the digests it depends on) and, for manifests and indexes, `body` (the exact source bytes, base64). Why bytes in the job and not a second download: the planner read them once, so a tag that moves between planning and fetching cannot mix versions, and no extra token is needed for manifests.
3. **SplitJson** `split jobs`: JsonPath Expression `$.jobs`. Auto-terminate `original`; `failure` to `hold`.
4. **EvaluateJsonPath** `job to attributes`: Destination `flowfile-attribute`, one dynamic property each: `kind` = `$.kind`, `repository` = `$.repository`, `digest` = `$.digest`, `size` = `$.size`, `tag` = `$.tag`, `reference` = `$.reference`, `layer.n` = `$.layer`, `layer.m` = `$.layers`, `platform` = `$.platform`, `check` = `$.check`, `transfer.id` = `$.transfer`, `media.type` = `$.mediaType`. `matched` on; `failure`, `unmatched` to `hold`.
5. **UpdateAttribute** `source settings`: `token.url` = `#{src_token_url}`. This is a quirk worth knowing: a `#{parameter}` is resolved in a plain property value but not inside a quoted expression literal such as `${literal('#{x}')}`, and not in `RouteOnAttribute` conditions. Every value a route or expression needs is therefore copied to an attribute first. `success` to `route by kind`. Then **RouteOnAttribute** `route by kind`, Routing Strategy `Route to Property name`: `blob` = `${kind:equals('blob'):or(${kind:equals('config')})}`, `manifest` = `${kind:equals('manifest'):or(${kind:equals('index')})}`. `unmatched` to `hold`.
6. **Blob branch, token decision.** The branch starts at **UpdateAttribute** `src token check` (section 9), then **RouteOnAttribute** `src needs token`: `reuse` = `${token.reuse:equals('true')}`, `mint` = `${token.url:startsWith('http'):and(${token.reuse:equals('true'):not()})}`. `reuse` and `unmatched` (no token service) go straight to `get blob`. `mint` to the minting processor.
7. **InvokeHTTP** `src mint token`: HTTP Method `GET`, HTTP URL `#{src_token_url}&scope=repository:${repository}:pull`, Request Username `#{src_user}`, Request Password `#{src_password}`, Response Body Attribute Name `auth.json`, Response Body Attribute Size `8192` (the default 256 truncates a token), Socket Read Timeout `30 secs`. Auto-terminate `Response`; `Original` to `src bearer header`; `Retry`, `No Retry`, `Failure` to `hold`. Then **UpdateAttribute** `src bearer header`: `auth.header` = `Bearer ${auth.json:jsonPath('$.token')}`, `auth.scope` = `repository:${repository}:pull` and `auth.expires` (section 9), `success` to `get blob`. The token exists only on the blob FlowFile, from here until `drop token`. It is never in a queued job. Honest limit: while it exists it is an attribute, so it is visible in the provenance events of those steps. It lasts about an hour and is scoped to one repository with pull only.
8. **InvokeHTTP** `get blob`: HTTP Method `GET`, HTTP URL `#{src_url}/v2/${repository}/blobs/${digest}`, a dynamic property `Authorization` = `${auth.header}` (empty when no token was minted), Connection Timeout `30 secs`, Socket Read Timeout `10 mins`, Socket Write Timeout `10 mins` (the default 15 s is far too short), Response Body Attribute Name empty, SSL Context Service if needed. The response body becomes the FlowFile content and streams: Python never sees it. Auto-terminate `Original` (the job; the planner can make it again). `Response` to `drop token` with Back Pressure Data Size Threshold `1 GB` and Object Threshold `10` on that connection. `Retry` (5xx) to `retry get`; `No Retry` (4xx) to **RouteOnAttribute** `src stage status` (`unauthorized` = `${invokehttp.status.code:equals('401')}` to **UpdateAttribute** `src expire token`, `auth.expires` = `0`, then `retry get`; anything else to `hold`); `Failure` to `hold`. Never auto-terminate them: a lost layer would vanish silently.
9. **RetryFlowFile** `retry get`: Maximum Retries `3`, Penalty Duration `30 sec`. `retry` back to `src token check`, so a retry mints a new token only when the old one has expired; `retries_exceeded`, `failure` to `hold`.
10. **UpdateAttribute** `drop token`: Delete Attributes Expression `(auth\..*|token\.reuse)`. The token is deleted here, before the hash and before `to diode`, so it never crosses to the high side. `success` to `hash content`.
11. **Manifest branch.** **EvaluateJsonPath** `manifest body`: Destination `flowfile-content`, one dynamic property `body` = `$.body`. The content becomes the base64 text. `matched` to `decode manifest`; `failure`, `unmatched` to `hold`. **EncodeContent** `decode manifest`: Mode `Decode`, Encoding `base64`. `success` to `hash content`, `failure` to `hold`.
12. **CryptographicHashContent** `hash content`: Hash Algorithm `SHA-256`. It reads a layer once more; the cost is one local read. `success` to `verify digest and size`, `failure` to `hold`.
13. **RouteOnAttribute** `verify digest and size`: `verified` = `${'content_SHA-256':equals(${digest:substringAfter(':')}):and(${fileSize:equals(${size})})}`. `verified` to the output port `to diode`; `unmatched` to `hold`. A layer that was corrupted in transit or that the source served wrongly never crosses.
14. **UpdateAttribute** `scrub` (Delete Attributes Expression `(auth\..*|token\.reuse)`, `success` to `hold`), then **LogMessage** `hold` (Log Level `warn`, Log Prefix `hold: ` with a trailing space, Log Message `discarded ${repository}@${digest}: ${registry.error:replaceNull(${execution.error})}`), auto-terminate `success`, and leave it **disabled**: enabling it logs each FlowFile as it discards it. Every connection called `hold` in this page really goes to `scrub`, so the token is deleted when a FlowFile is handed to the hold queue. Every failure connection ends here, so a FlowFile keeps its content until you list the queue, fix the cause and move it on.
15. **Output port** `to diode`. The connection to the other side uses Load Balance Strategy `Round robin` if the high side is a cluster, Prioritizer `NewestFlowFileFirstPrioritizer` (small manifests overtake layers) and Back Pressure Data Size Threshold `1 GB`. A diode replaces this connection with your existing transfer mechanism, which must carry the attributes (`PackageFlowFile` and `UnpackContent` do).

Start every processor except `hold` and `request`.

## 3. High side (group `native layer flow high`)

1. **Input port** `from diode`, then **UpdateAttribute** `target settings`: `token.url` = `#{dst_token_url}`, `dst.url` = `#{dst_url}`, `dst.repository` = `#{dst_repository}`. Then **UpdateAttribute** `target repository`: `repository` = `${dst.repository:isEmpty():ifElse(${repository}, ${dst.repository})}` (an empty `dst_repository` keeps the source name) and `dst.origin` = `${dst.url:substringBefore('://'):append('://'):append(${dst.url:substringAfter('://'):substringBefore('/')})}` (scheme and host only, for relative upload addresses). Then **RouteOnAttribute** `route by kind` as on the low side.
2. **Blob branch.** `dst token check`, `dst needs token`, `dst mint token`, `dst bearer header` and `dst expire token` as on the low side (section 9) with the `dst_` parameters and scope `pull,push`: HTTP URL `#{dst_token_url}&scope=repository:${repository}:pull,push`.
3. **InvokeHTTP** `head blob`: HTTP Method `HEAD`, HTTP URL `#{dst_url}/v2/${repository}/blobs/${digest}`, `Authorization` = `${auth.header}`, Socket Read Timeout `60 secs`. Auto-terminate `Response`. `Original` and `No Retry` to `exists or missing`; `Retry` to `retry upload`, `Failure` to `hold`.
4. **RouteOnAttribute** `exists or missing`: `exists` = `${invokehttp.status.code:equals('200')}`, `missing` = `${invokehttp.status.code:equals('404')}`. `unauthorized` = `${invokehttp.status.code:equals('401')}` to `dst expire token` then `retry upload`. `unmatched` (403, anything else) to `hold`. `exists` to **UpdateAttribute** `skip existing` (Delete Attributes Expression `auth\..*`, auto-terminate `success`): the destination already has the blob, so the copy is dropped on purpose and the layer is never uploaded twice.
5. **Upload, two requests.** The first needs no body and the second carries the blob, so no registry has to support anything beyond the spec.
   1. **InvokeHTTP** `start upload`: HTTP Method `POST`, HTTP URL `#{dst_url}/v2/${repository}/blobs/uploads/`, Request Body Enabled `false`, `Authorization` = `${auth.header}`, Response Header Request Attributes Enabled `true` and Response Header Request Attributes Prefix `resp.` (this copies the response headers onto the blob FlowFile, which keeps its content; without it the headers land only on the empty response FlowFile). Back Pressure Data Size Threshold `1 GB` on the `missing` connection into it. Auto-terminate `Response`. `Original` (202) to `upload url`; `Retry` to `retry upload`, `No Retry` to `dst stage status` (401 to `dst expire token`, anything else to `hold`), `Failure` to `hold`.
   2. **UpdateAttribute** `upload url`: `upload.ok` = `${resp.Location:replaceNull(${resp.location}):startsWith('/'):or(${resp.Location:replaceNull(${resp.location}):startsWith(${dst.origin:append('/')})})}` and `upload.url` = `${resp.Location:replaceNull(${resp.location}):startsWith('/'):ifElse(${dst.origin}, ''):append(${resp.Location:replaceNull(${resp.location})}):append(${resp.Location:replaceNull(${resp.location}):contains('?'):ifElse('&', '?')}):append('digest='):append(${digest})}`. The `Location` header comes in every form: relative or absolute, with or without a query, and with the name lower-cased on HTTP/2 (hence `replaceNull(${resp.location})`). Measured on the registries used here: `registry:2` absolute with a `?_state=` query (the host comes from the request, so it equals `dst_url`), zot relative with no query, Quay absolute (`https://<host>/v2/<repo>/blobs/uploads/<uuid>`), lower-case `resp.location` over HTTP/2. The two expressions turn each of them into a full URL with `digest=` appended after `?` or `&`.
      Read both spellings of the header. A registry that answers over HTTP/2, such as `registry:2` (CNCF Distribution) behind TLS, sends header names in lower case, so InvokeHTTP writes `resp.location`; over HTTP/1.1 it writes `resp.Location`. Reading only `${resp.Location}` leaves `upload.url` without a scheme, and `put blob` fails with `Expected URL scheme 'http' or 'https' but no scheme was found`. Setting `HTTP/2 Disabled` to `True` on the InvokeHTTP processors also avoids it.
   3. **RouteOnAttribute** `upload url ok`: `ok` = `${upload.ok:equals('true')}`. `unmatched`, meaning an address on another host, goes to `hold`: credentials are only ever sent to `dst_url`.
   4. **InvokeHTTP** `put blob`: HTTP Method `PUT`, HTTP URL `${upload.url}`, Request Body Enabled `true`, Request Content-Type `application/octet-stream`, `Authorization` = `${auth.header}`, Connection Timeout `30 secs`, Socket Read Timeout and Socket Write Timeout `10 mins`. Auto-terminate `Response`. `Original` (201) to **UpdateAttribute** `blob done` (Delete Attributes Expression `(auth\..*|resp\..*|token\.reuse)`, auto-terminate `success`). `Retry` to `retry upload`, `No Retry` to `dst stage status`, `Failure` to `hold`.
6. **Manifest branch.** **ExecuteStreamCommand** `manifest gate`: Command Path `python3`, Command Arguments `#{gate_script};#{dst_url};${repository};${reference};25;${platform}`, Argument Delimiter `;`, Ignore STDIN `false`, Output Destination Attribute `gate.result`, dynamic property `REGISTRY_USER` = `#{dst_user}` and sensitive dynamic property `REGISTRY_PASSWORD` = `#{dst_password}` (tick Sensitive when adding it). The FlowFile content is the raw manifest or index. [manifest_gate.py](../../python/scripts/manifest_gate.py) polls `HEAD` for the config and every layer (for an index, every child manifest) for up to 25 s, then `PUT`s the stdin bytes unchanged with the document's own `mediaType`. It mints its own token when the registry wants one. Only `original` is left, so read the result in `execution.status`.
7. **RouteOnAttribute** `gate status`: `done` = `${execution.status:equals('0')}`, `incomplete` = `${execution.status:equals('5')}`. `unmatched` (14 manifest refused, 10 credentials refused, 15 bad arguments) to `hold`. `done` to **UpdateAttribute** `manifest done` (auto-terminate `success`).
8. **LogMessage** `incomplete bulletin`: Log Level `warn`, Log Prefix `incomplete`, Log Message `${execution.error}`. Its output goes to **RetryFlowFile** `retry gate`: Retry Attribute `gate.retries`, Maximum Retries `120`, Penalty Duration `30 sec`, so the gate runs again every 30 s for about an hour. `retry` back to `manifest gate`; `retries_exceeded`, `failure` to `hold`.
9. **RetryFlowFile** `retry upload`: Retry Attribute `upload.retries`, Maximum Retries `3`, Penalty Duration `30 sec`; `retry` to `dst token check` (it restarts at the `HEAD`, which is idempotent), `retries_exceeded`, `failure` to `hold`. **UpdateAttribute** `scrub` and **LogMessage** `hold` (disabled), as on the low side.

Order of arrival does not matter: a manifest that arrives before its layers waits in the gate loop. An index is pushed only after its child manifests are present, children first.

## 4. Recovery of a lost layer

1. High side: read the bulletin, for example `team/app:1.0 incomplete: missing layers 2 and 3 of 4. Check code 677a46bf`, or `... (linux/arm64) incomplete: missing platform manifests 1 of 2 ...`, or `... and the config blob`. Note the image, the platform if one is shown, the layer numbers and the check code.
2. Low side: stop nothing. Open `request` > Configure > Properties, and add `registry.layers` = `2,3`, `registry.check` = `677a46bf`, and for an image with an index also `registry.platform` = `linux/arm64`. Run `request` once, then remove the three properties again.
3. The planner re-resolves the tag, recomputes the check code and refuses when it differs: `refused: check code deadbeef does not match 677a46bf for team/app:1.0, the tag has moved; nothing was sent`. A moved tag needs a fresh full export, not a resend. Otherwise it plans only the config and the named layers, no manifest, and the flow sends just those. The waiting manifest passes the gate at its next 30 s retry and the bulletin stops.
4. A layer FlowFile stuck in a `hold` queue (not lost) needs no resend: fix the cause, draw a connection from `hold` (`success`, untick its auto-terminate) to the processor that failed, start `hold` until the queue is empty, stop it, and remove that connection again.

## 5. TLS

Set the **SSL Context Service** on the planner and on every `InvokeHTTP`: a `StandardSSLContextService` whose truststore holds the registry CA (PKCS12 or JKS). The gate script uses the system trust store of the node, or `SSL_CERT_FILE` in the NiFi environment. Never turn certificate verification off.

## 6. Import instead of building

1. On each canvas: drag **Process Group** onto the canvas, click the browse icon beside the name field and choose the matching file from [../flows/](../flows/). The low definition goes to the low NiFi, the high definition to the high NiFi.
2. Open Parameters: the import creates `registry-native-layer` with the names above and empty or placeholder values. Fill them in, then set the two sensitive passwords.
3. Right-click the group > **Enable all controller services** if you add an SSL Context Service, then start all processors except `hold` and `request`.
4. In `request`, set `registry.image.reference` and run it once.

## 7. Check it

- `skopeo inspect --raw docker://registry-high.example.com/team/app:1.0 > high.json`, the same against the source, then `cmp`: byte-identical. Compare the children of an index by digest too.
- Run the same image again: every layer stops at `skip existing` and no `put blob` event is written.
- Lab evidence, NiFi 2.12.0 with a one-gigabyte heap, an image of three 3 GiB layers to `registry:2` (distribution 2.8.3) with the two-request upload: all layers and the manifest crossed in about 22 s, the manifest digest and bytes equal the source, the Python extension process peaked at 32 MB (it only plans), NiFi peaked at 1.4 GB resident and the content repository at 9.2 GB (one copy, archive off). An OCI index with two children was byte-identical on `registry:2` and on zot. A lost-layer resend and a rerun that skips every layer were also proven.
- Prod evidence, NiFi 2.12.0 on three nodes, Quay as source and target, small images (tags with two to four layers and an OCI index with two children): every manifest, child and the index byte-identical, 14 of 14 layers present with the right size, a rerun skipped all of them. The token existed on the blob FlowFile for the steps between `src bearer header` and `drop token` and was gone after them.
- Memory and disk: the content repository holds each layer once per side while it is queued, so size the volume for the largest image times the back pressure you allow. Keep Concurrent Tasks at 1 on `get blob` and `put blob` until a single large image has passed.

## 8. Optional shortcut: one request per layer

Quay and zot accept `POST /v2/<repo>/blobs/uploads/?digest=<d>` with the blob as the body and answer 201. To use it, replace `start upload`, `upload url`, `upload url ok` and `put blob` with one **InvokeHTTP** `post blob`: HTTP Method `POST`, HTTP URL `#{dst_url}/v2/${repository}/blobs/uploads/?digest=${digest}`, Request Body Enabled `true`, Request Content-Type `application/octet-stream`, `Authorization` = `${auth.header}`, long timeouts as above, `Original` to `blob done`. It saves one request per layer and the Location handling, but `registry:2` does not support it: it answers without reading the body and the processor fails with `Broken pipe`. Keep the two-request form unless every target is known to support the shortcut.

## 9. Token lifetime, reuse and retries

A token is minted once per blob stage group (the pull token before `get blob`, the pull,push token before `head blob`), kept as FlowFile attributes only while that stage may need it, and deleted on success or when the FlowFile is handed to `hold`. It never crosses `to diode`.

- `bearer header` sets three attributes: `auth.header` (`Bearer ...`), `auth.scope` (`repository:${repository}:pull` or `pull,push`) and `auth.expires` = `${now():toNumber():plus(${auth.json:jsonPath('$.expires_in'):replaceEmpty('3600'):toNumber():multiply(1000)}):minus(300000)}`, epoch milliseconds, 300 s before the real expiry, 3600 s when the registry sends no `expires_in`.
- `token check` (UpdateAttribute) sets `token.reuse` to `${auth.expires:isNull():not():and(${auth.expires:gt(${now():toNumber()})}):and(${auth.scope:equals(${literal('repository:'):append(${repository}):append(':pull')})})}` (`:pull,push` on the high side). True only when the FlowFile holds an unexpired token for this repository and scope.
- `needs token` routes `reuse` straight to the stage and `mint` to the mint stage. Both outputs are exclusive on purpose: `RouteOnAttribute` sends a FlowFile to every matching relationship, which would clone it.
- Every retry loop (`retry get`, `retry upload`) goes back to `token check`, so a 5xx retry reuses a valid token and only mints again after expiry.
- A 401 from a stage means the token expired early or was revoked: `expire token` sets `auth.expires` to `0` and the retry then mints. The retry counters bound the loop at three attempts.
- Limit: while a token exists it is in the attributes of the FlowFile and in the provenance events of those steps. Its lifetime is about an hour and it is scoped to one repository.
