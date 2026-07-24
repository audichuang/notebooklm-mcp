# Feed Deployment is a logically separate aggregate

Feed Deployment is logically separate from Episode and Generation Attempt, while v1 stores it in the same manifest under the same atomic `ManifestStore`; no database or service is added. Each deployment persists a complete authoritative feed snapshot before its first remote side effect, including every item’s attempt identity where known, exact public-enclosure bytes hash, GUID, URLs, attachments, and artwork; legacy items remain explicit rather than being omitted.

Every upload records intent, content identity, and an `outcome_unknown` path that reconciles the predetermined public URL instead of blindly retrying. An interrupted deployment is neither rolled back nor published: it remains incomplete and must be reconciled under a per-show deployment lock. `latest_verified_deployment_id` changes only after the complete public feed and all referenced assets have been read back and their verification evidence saved.
