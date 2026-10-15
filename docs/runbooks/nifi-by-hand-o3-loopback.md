# Building the O3 loopback flow by hand: ListenHTTP on 127.0.0.1

A script streams a blob from the registry straight into a `ListenHTTP` on the same node, so the layer becomes FlowFile content with no staging disk. Choice and evidence: [ADR 0006](../decisions/adr-0006-moving-large-images-through-nifi.md), option O3. Hosts are placeholders: `registry.example.com`, NiFi cert name `nifi-loopback`.

```text
GenerateFlowFile (primary node) -> ExecuteStreamCommand (poster script) -> success: marker FlowFile
                                                          POST https://127.0.0.1:18100/blobs
ListenHTTP (Base Path blobs, mTLS) -> blob FlowFile (native content)
```

## 1. SSL context service

Create a `StandardSSLContextService` in the process group (or controller level):

| Property | Value |
|---|---|
| Keystore Filename, Type, Password | a keystore holding one certificate whose CN is the name the client will send, `nifi-loopback` |
| Truststore Filename, Type, Password | a truststore holding that certificate's CA |
| TLS Protocol | `TLS` |

Enable it. Every node needs the keystore at the same path. A sensitive password comes from a parameter, not typed into the property.

## 2. ListenHTTP

| Property | Value |
|---|---|
| Base Path | `blobs` |
| Listening Port | `18100` |
| SSL Context Service | the service above |
| Client Authentication | `REQUIRED` |
| Authorized Subject DN Pattern | `CN=nifi-loopback.*` |
| HTTP Headers to receive as Attributes (Regex) | `X-.*\|filename` |
| Max Unconfirmed Flowfile Time | default |

`ListenHTTP` has no bind-address property, so it listens on every interface and another pod can reach `pod-IP:18100`. The three tests that matter: no client certificate is refused at the handshake, a certificate with another DN gets 403, the right certificate gets 200. Do not protect it with a shared token in a header: header values are attributes, so the token would appear in provenance.

## 3. GenerateFlowFile (trigger)

Scheduling: **Execution** `Primary node`. Custom Text empty. Add attributes as dynamic properties: `dst.repo`, `blob.digest`, `X-Layer-N`, `X-Layer-M`, `X-Image-Id`.

## 4. ExecuteStreamCommand (poster)

| Property | Value |
|---|---|
| Command Path | `python3` |
| Command Arguments | `/opt/nifi/scripts/post_blob_loopback.py;https://registry.example.com;${dst.repo};${blob.digest};18100;--path;blobs;--name;nifi-loopback;--ca;/etc/nifi/loopback/ca.pem;--cert;/etc/nifi/loopback/client.pem;--key;/etc/nifi/loopback/client.key;--header;X-Image-Id=${X-Image-Id};--header;X-Layer-N=${X-Layer-N};--header;X-Layer-M=${X-Layer-M};--attempts;6` |
| Argument Delimiter | `;` |
| Ignore STDIN | `true` |
| Output Destination Attribute | `post.result` |

Dynamic properties: `REGISTRY_USER` and a sensitive `REGISTRY_PASSWORD`, as in [nifi-by-hand-o4-push.md](nifi-by-hand-o4-push.md) section 5. The script is [post_blob_loopback.py](../../python/scripts/post_blob_loopback.py) (it needs `push_from_stdin.py` beside it). Per try it:

1. Gets the blob from the registry (a Bearer token with `pull` scope when the registry asks for one) and reads it in 1 MiB chunks. A redirect to object storage is followed without the registry credentials (at most 3 hops, http or https only, never plain http from an https registry). The address it connects to must be public; an on-premises store such as MinIO on a private address needs `--trusted-storage minio.internal,10.20.0.0/16` (or `LOOPBACK_TRUSTED_STORAGE`). Any other redirect exits 15 and nothing is posted.
2. POSTs it as a chunked request to `https://127.0.0.1:<port>/<path>` with `Content-Type: application/octet-stream`, your `--header NAME=VALUE` pairs, `X-Blob-Digest`, `filename` and `X-Attempt`, using the client certificate for mTLS.
3. Sends `--name` (the certificate name) as both the TLS SNI and the `Host` header while connecting to 127.0.0.1, because Jetty answers `400 Invalid SNI` for an IP address.
4. Retries with waits of 2, 4, 8 ... up to 30 s, up to `--attempts` tries (section 6).

The certificate files are PEM: `--ca` signed the ListenHTTP certificate, `--cert` and `--key` are the client pair whose subject matches the Authorized Subject DN Pattern. Exit `0` delivered, `6` not delivered after all tries, `10` credentials refused, `15` bad arguments or an unreadable TLS file. The unit tests run it over plain http against a local server; the TLS path is covered by the live proof in section 7.

## 5. Marker and correlation

The poster's output FlowFile (the marker) carries `image.id`. Each landed blob FlowFile carries `X-Image-Id` (the same value), `X-Layer-N` of `X-Layer-M` and `X-Blob-Digest`. A collector counts blobs per `X-Image-Id`; the join was not built.

## 6. Back pressure and retries

`ListenHTTP` checks the downstream queue only on every 5th request (`FILES_BEFORE_CHECKING_DESTINATION_SPACE = 5`, SOURCE). When the queue is over its threshold it then answers 503 before reading the body, and a client that is still sending sees a closed connection (`SSLEOFError`), not a clean 503. The connection after `ListenHTTP` can overshoot its threshold by up to 5 FlowFiles, so size Back Pressure Object Threshold with that margin. The poster retries (`--attempts 6` with backoff delivered on attempt 4 after the consumer started); each retry reads the blob from the registry again, and `X-Attempt` tells the FlowFile which try landed.

A stop in the middle of an upload leaves a ListenHTTP bulletin (`EofException`) and no partial FlowFile.

## 7. Check it

1. Run the trigger once. A blob FlowFile appears after `ListenHTTP`, with the `X-Blob-Digest` attribute.
2. Its size equals the registry's `Content-Length`. For a check of content, use **Download** from the queue and `sha256sum` it: the result equals `blob.digest`.
3. Content repository grew by one copy of the blob, with no staging directory.

Proven live by the lead on the 3-node cluster over mTLS (ListenHTTP Client Authentication `REQUIRED`, an Authorized Subject DN pattern, `--name` set to the certificate name, PEM `--ca`, `--cert` and `--key` from the client keystore directory): a 5,244,741-byte Quay blob was delivered on attempt 1 in 0.1 s. The landed FlowFile was 5,244,741 bytes with `X-Blob-Digest`, `filename`, `X-Attempt` and a custom header as attributes, and the sha256 of its content equals the digest. This covers the TLS and SNI path.

## 8. Cluster rule

The trigger runs on the primary node and posts to `127.0.0.1`, so the blob lands on that node. Anything that later needs the bytes (such as the push) runs on that node unless a load-balanced connection moves them. A load-balanced layout was not tested.
