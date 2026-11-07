Type generation uses `services/api/schema/openapi.json`, exported from the API's
Pydantic contracts. It does not infer types from fixtures or handwritten enums.

From the repository root, after exporting the Python schema:

```powershell
npm --prefix apps/web ci
npm --prefix apps/web run generate:types
npm --prefix apps/web run check:types
npm --prefix apps/web run typecheck
```

`generate:types` writes `src/generated/contracts.ts`. `check:types` exits with an
error if that file differs from fresh generation. Neither command starts the API
or contacts a provider. The generation input is the local schema export; keep
that export current by running `python scripts/export_contracts.py` from
`services/api` before regenerating. Exported contract types do not establish that
the corresponding HTTP routes have been implemented.

TypeScript checks static structure, not runtime JSON validity or all Pydantic
cross-field rules. Runtime responses still require validation at their owning
boundary.
