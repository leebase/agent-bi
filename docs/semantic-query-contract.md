# SemanticQuery contract

A SemanticQuery (`semantic-query/1.0`) is the only thing a producer writes. It names
objects of a semantic model; it never carries SQL, a number, or any executable text.
`agent-bi` validates it, hashes it, and compiles it deterministically (architecture
section 4.3 and 5.3).

## Example

```json
{
  "schema_version": "semantic-query/1.0",
  "model": "idle_warehouse_waste",
  "table": "warehouse_metering_history",
  "metrics": ["total_credits_used"],
  "dimensions": ["warehouse_name"],
  "time": {"dimension": "usage_day", "grain": "day", "start": "2026-01-01", "end": "2026-01-31"},
  "filters": [{"dimension": "warehouse_name", "op": "in", "values": ["ANALYTICS_WH"]}],
  "named_filters": [],
  "order_by": [{"field": "total_credits_used", "direction": "desc"}],
  "limit": 1000
}
```

## Rules

- `metrics` has at least one entry, unless `dimensions` has at least one and `metrics` is
  `[]` (a pure dimension listing, compiled as `SELECT DISTINCT`).
- Every name must exist in the model's single table. An unknown name is
  `E_UNKNOWN_FIELD`, and the message lists the valid names.
- `time` is optional. When present, `dimension` must be a time dimension and `grain` is one
  of `day`, `week`, `month`, `quarter`, `year`. `start` and `end` are ISO dates (`YYYY-MM-DD`);
  `start` is inclusive and `end` is exclusive.
- `filters[].op` is one of `=`, `!=`, `in`, `not_in`, `>`, `>=`, `<`, `<=`, `between`, `like`.
  `values` is a list: exactly one value, except `in` and `not_in` (one or more) and
  `between` (exactly two).
- `order_by[].field` must be a selected metric, a selected dimension, or `time_bucket`
  (only when `time` is present). `direction` is `asc` or `desc`.
- `limit` defaults to 1000; the maximum is 100000.
- A name repeated within `metrics`, `dimensions` or `named_filters` is refused
  (`E_QUERY_INVALID` naming it), never silently collapsed.
- Unknown keys are rejected. A query is at most 1 MB.
- Any contract violation is `E_QUERY_INVALID` (exit 2).

## Canonical form and identity

The canonical form has sorted keys; `metrics`, `dimensions` and `named_filters` sorted;
`filters` sorted by `(dimension, op, values)`; `order_by` kept in
order; no whitespace. `query_hash` is `sha256:` plus the SHA-256 of the canonical JSON.
The compiler consumes the canonical form only, so two inputs with the same `query_hash`
always compile to the same SQL text.

The generated JSON Schema below is the authoritative machine-readable form.

```json
{
  "$defs": {
    "FilterSpec": {
      "additionalProperties": false,
      "properties": {
        "dimension": {
          "title": "Dimension",
          "type": "string"
        },
        "op": {
          "enum": [
            "=",
            "!=",
            "in",
            "not_in",
            ">",
            ">=",
            "<",
            "<=",
            "between",
            "like"
          ],
          "title": "Op",
          "type": "string"
        },
        "values": {
          "items": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "boolean"
              },
              {
                "type": "integer"
              },
              {
                "type": "number"
              }
            ]
          },
          "title": "Values",
          "type": "array"
        }
      },
      "required": [
        "dimension",
        "op",
        "values"
      ],
      "title": "FilterSpec",
      "type": "object"
    },
    "OrderSpec": {
      "additionalProperties": false,
      "properties": {
        "direction": {
          "default": "asc",
          "enum": [
            "asc",
            "desc"
          ],
          "title": "Direction",
          "type": "string"
        },
        "field": {
          "title": "Field",
          "type": "string"
        }
      },
      "required": [
        "field"
      ],
      "title": "OrderSpec",
      "type": "object"
    },
    "TimeSpec": {
      "additionalProperties": false,
      "properties": {
        "dimension": {
          "title": "Dimension",
          "type": "string"
        },
        "end": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "End"
        },
        "grain": {
          "enum": [
            "day",
            "week",
            "month",
            "quarter",
            "year"
          ],
          "title": "Grain",
          "type": "string"
        },
        "start": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Start"
        }
      },
      "required": [
        "dimension",
        "grain"
      ],
      "title": "TimeSpec",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "dimensions": {
      "items": {
        "type": "string"
      },
      "title": "Dimensions",
      "type": "array"
    },
    "filters": {
      "items": {
        "$ref": "#/$defs/FilterSpec"
      },
      "title": "Filters",
      "type": "array"
    },
    "limit": {
      "default": 1000,
      "maximum": 100000,
      "minimum": 1,
      "title": "Limit",
      "type": "integer"
    },
    "metrics": {
      "items": {
        "type": "string"
      },
      "title": "Metrics",
      "type": "array"
    },
    "model": {
      "title": "Model",
      "type": "string"
    },
    "named_filters": {
      "items": {
        "type": "string"
      },
      "title": "Named Filters",
      "type": "array"
    },
    "order_by": {
      "items": {
        "$ref": "#/$defs/OrderSpec"
      },
      "title": "Order By",
      "type": "array"
    },
    "schema_version": {
      "const": "semantic-query/1.0",
      "default": "semantic-query/1.0",
      "title": "Schema Version",
      "type": "string"
    },
    "table": {
      "title": "Table",
      "type": "string"
    },
    "time": {
      "anyOf": [
        {
          "$ref": "#/$defs/TimeSpec"
        },
        {
          "type": "null"
        }
      ],
      "default": null
    }
  },
  "required": [
    "model",
    "table"
  ],
  "title": "SemanticQuery",
  "type": "object"
}
```
