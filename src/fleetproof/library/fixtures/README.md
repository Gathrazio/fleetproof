# Library fixtures

`http_json_field.sample.json` is an EXAMPLE of the shape `http_json_field.py`
grades, so the check can be exercised before a target exists. It was written
by FleetProof's authors, which makes it exactly the kind of sample that must
never stand as your positive control: replace it with your own captured emission
of the real endpoint, then register it with
`fleetproof check control http-json-field --pass-sample <captured.json> --provenance captured`.
Each script's header says how to capture a sample for it.
