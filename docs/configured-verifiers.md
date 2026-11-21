# Configurable independent verification

`praxis run ... --verifier rules.json` accepts mixed operator-owned rules:

```json
{
  "root": ".",
  "allowed_http_origins": ["https://app.example"],
  "criteria": {
    "app open": {"kind":"dom","role":"heading","name":"Reports"},
    "quantity set": {"kind":"dom","role":"textbox","name":"Quantity","value":"7"},
    "server finished": {"kind":"http_json","url":"https://app.example/result","json_fields":{"/status":"done"}},
    "database committed": {"kind":"sqlite","path":"app.db","query":"SELECT status FROM jobs WHERE id = ?","parameters":[7],"rows":[["complete"]]},
    "application ready": {"kind":"app_state","json_fields":{"/ready":true}},
    "region unchanged": {"kind":"screenshot_region","baseline":"expected.png","region":[0,0,100,100],"max_changed_fraction":0.01},
    "report valid": {"path":"report.json","fresh":true,"json_fields":{"/status":"done"}}
  }
}
```

DOM and screenshot checks bind to the active browser. Application checks call a
trusted backend's `verification_state()` method, never model-supplied evidence.
Screenshot checks require `pip install -e ".[visual]"` and compare actual pixels
inside a bounded region; they do not infer UI semantics. Baseline images and all
database/artifact paths stay inside the configured root.

HTTP checks issue GET only to explicitly configured origins, reject redirects,
and bound response size/time. SQLite checks open existing databases read-only,
authorize SELECT and column reads only, and bound execution time and rows. They
support column queries, not SQL functions. Returned evidence contains outcome
metadata and hashes rather than response bodies or database contents.

Mixed checks report `custom_verifier` provenance. Existing file-only configurations
keep `independent_artifact`. Backend-dependent checks cannot pass through standalone
`praxis verify` without an active backend. File-based experience curation remains
separate; mixed checks are not silently relabelled as file-only learning evidence.

For custom `VerificationRegistry` callbacks, supply a new explicit `revision` when
changing implementation. Criteria rules and revision identities are bound to the
checkpoint and must match on resume.
