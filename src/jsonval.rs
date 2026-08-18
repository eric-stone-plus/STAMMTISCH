//! Minimal JSON Schema validator — no validator crate.
//!
//! Deliberately tiny subset, sufficient for the STAMMTISCH contracts and
//! the doctrine domain schemas:
//! `type`, `required`, `properties`, `additionalProperties` (bool or
//! schema), `items` (single schema), `uniqueItems`, `enum`, `const`,
//! `pattern` (via [`crate::pattern`]), `minLength`, `maxLength`, `minItems`,
//! `maxItems`, `minimum`, `maximum`, the applicators `allOf`, `oneOf`, and
//! `if`/`then`/`else`, and local `$ref` (`#/...`). `format` and other
//! annotation keywords are ignored, matching draft 2020-12 default
//! behavior. Unsupported `pattern` constructs surface as validation
//! errors — the caller fails closed.

use serde_json::Value;

/// Validate `instance` against `schema`. Returns the list of violations;
/// empty means valid.
pub fn violations(schema: &Value, instance: &Value) -> Vec<String> {
    let mut errs = Vec::new();
    validate(schema, schema, instance, "$", &mut errs, 0);
    errs
}

pub fn is_valid(schema: &Value, instance: &Value) -> bool {
    violations(schema, instance).is_empty()
}

const MAX_DEPTH: usize = 64;

fn validate(
    root: &Value,
    schema: &Value,
    inst: &Value,
    path: &str,
    errs: &mut Vec<String>,
    depth: usize,
) {
    if depth > MAX_DEPTH {
        errs.push(format!("{path}: schema recursion depth exceeded"));
        return;
    }
    let obj = match schema.as_object() {
        Some(o) => o,
        None => {
            errs.push(format!("{path}: schema is not an object"));
            return;
        }
    };

    if let Some(reference) = obj.get("$ref").and_then(Value::as_str) {
        match resolve_ref(root, reference) {
            Some(target) => validate(root, target, inst, path, errs, depth + 1),
            None => errs.push(format!("{path}: unresolvable $ref {reference}")),
        }
        return;
    }

    if let Some(types) = obj.get("type") {
        let ok = match types {
            Value::String(t) => type_matches(t, inst),
            Value::Array(ts) => ts
                .iter()
                .filter_map(Value::as_str)
                .any(|t| type_matches(t, inst)),
            _ => {
                errs.push(format!("{path}: malformed 'type' keyword"));
                true
            }
        };
        if !ok {
            errs.push(format!(
                "{path}: expected type {}, got {}",
                types,
                type_name(inst)
            ));
            return; // deeper checks would be noise
        }
    }

    if let Some(c) = obj.get("const") {
        if c != inst {
            errs.push(format!("{path}: const mismatch (expected {c})"));
        }
    }
    if let Some(e) = obj.get("enum").and_then(Value::as_array) {
        if !e.contains(inst) {
            errs.push(format!("{path}: value not in enum"));
        }
    }

    // Applicators (draft 2020-12): evaluated regardless of instance type.
    if let Some(all) = obj.get("allOf").and_then(Value::as_array) {
        for sub in all {
            validate(root, sub, inst, path, errs, depth + 1);
        }
    }
    if let Some(one) = obj.get("oneOf").and_then(Value::as_array) {
        let mut matched = 0usize;
        for sub in one {
            let mut sub_errs = Vec::new();
            validate(root, sub, inst, path, &mut sub_errs, depth + 1);
            if sub_errs.is_empty() {
                matched += 1;
            }
        }
        if matched != 1 {
            errs.push(format!(
                "{path}: matches {matched} oneOf branches (exactly one required)"
            ));
        }
    }
    if let Some(if_schema) = obj.get("if") {
        let mut if_errs = Vec::new();
        validate(root, if_schema, inst, path, &mut if_errs, depth + 1);
        let branch = if if_errs.is_empty() {
            obj.get("then")
        } else {
            obj.get("else")
        };
        if let Some(branch) = branch {
            validate(root, branch, inst, path, errs, depth + 1);
        }
    }

    match inst {
        Value::Object(map) => {
            if let Some(req) = obj.get("required").and_then(Value::as_array) {
                for key in req.iter().filter_map(Value::as_str) {
                    if !map.contains_key(key) {
                        errs.push(format!("{path}: missing required property '{key}'"));
                    }
                }
            }
            let props = obj.get("properties").and_then(Value::as_object);
            for (key, val) in map {
                let sub = props.and_then(|p| p.get(key));
                match (sub, obj.get("additionalProperties")) {
                    (Some(s), _) => {
                        validate(root, s, val, &format!("{path}.{key}"), errs, depth + 1)
                    }
                    (None, Some(Value::Bool(false))) => {
                        errs.push(format!("{path}: additional property '{key}' not allowed"));
                    }
                    (None, Some(ap @ Value::Object(_))) => {
                        validate(root, ap, val, &format!("{path}.{key}"), errs, depth + 1)
                    }
                    _ => {}
                }
            }
        }
        Value::Array(items) => {
            if let Some(min) = obj.get("minItems").and_then(Value::as_u64) {
                if (items.len() as u64) < min {
                    errs.push(format!("{path}: fewer than {min} items"));
                }
            }
            if let Some(max) = obj.get("maxItems").and_then(Value::as_u64) {
                if items.len() as u64 > max {
                    errs.push(format!("{path}: more than {max} items"));
                }
            }
            if obj.get("uniqueItems").and_then(Value::as_bool) == Some(true) {
                let mut seen = std::collections::HashSet::new();
                for (i, item) in items.iter().enumerate() {
                    if !seen.insert(crate::canon::canonical(item)) {
                        errs.push(format!("{path}: duplicate item at index {i} (uniqueItems)"));
                    }
                }
            }
            if let Some(item_schema) = obj.get("items") {
                for (i, item) in items.iter().enumerate() {
                    validate(
                        root,
                        item_schema,
                        item,
                        &format!("{path}[{i}]"),
                        errs,
                        depth + 1,
                    );
                }
            }
        }
        Value::String(s) => {
            let len = s.chars().count() as u64;
            if let Some(min) = obj.get("minLength").and_then(Value::as_u64) {
                if len < min {
                    errs.push(format!("{path}: shorter than minLength {min}"));
                }
            }
            if let Some(max) = obj.get("maxLength").and_then(Value::as_u64) {
                if len > max {
                    errs.push(format!("{path}: longer than maxLength {max}"));
                }
            }
            if let Some(p) = obj.get("pattern").and_then(Value::as_str) {
                match crate::pattern::matches(p, s) {
                    Ok(true) => {}
                    Ok(false) => errs.push(format!("{path}: does not match pattern {p}")),
                    Err(e) => errs.push(format!("{path}: pattern error: {e}")),
                }
            }
        }
        Value::Number(n) => {
            if let Some(min) = obj.get("minimum").and_then(Value::as_f64) {
                if n.as_f64().map(|x| x < min).unwrap_or(true) {
                    errs.push(format!("{path}: below minimum {min}"));
                }
            }
            if let Some(max) = obj.get("maximum").and_then(Value::as_f64) {
                if n.as_f64().map(|x| x > max).unwrap_or(true) {
                    errs.push(format!("{path}: above maximum {max}"));
                }
            }
        }
        _ => {}
    }
}

fn resolve_ref<'a>(root: &'a Value, reference: &str) -> Option<&'a Value> {
    let pointer = reference.strip_prefix('#')?;
    if pointer.is_empty() {
        return Some(root);
    }
    root.pointer(pointer)
}

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "boolean",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

fn type_matches(t: &str, v: &Value) -> bool {
    match t {
        "null" => v.is_null(),
        "boolean" => v.is_boolean(),
        "number" => v.is_number(),
        "integer" => v.as_f64().map(|f| f.fract() == 0.0).unwrap_or(false),
        "string" => v.is_string(),
        "array" => v.is_array(),
        "object" => v.is_object(),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn object_required_and_additional() {
        let schema = json!({
            "type": "object",
            "required": ["a"],
            "additionalProperties": false,
            "properties": {"a": {"type": "integer", "minimum": 1}}
        });
        assert!(is_valid(&schema, &json!({"a": 2})));
        assert!(!is_valid(&schema, &json!({}))); // missing required
        assert!(!is_valid(&schema, &json!({"a": 1, "b": 2}))); // additional
        assert!(!is_valid(&schema, &json!({"a": 0}))); // below minimum
        assert!(!is_valid(&schema, &json!({"a": 1.5}))); // not integer
    }

    #[test]
    fn local_ref_resolution() {
        let schema = json!({
            "type": "object",
            "properties": {"s": {"$ref": "#/$defs/stage"}},
            "$defs": {"stage": {"type": "object", "required": ["id"]}}
        });
        assert!(is_valid(&schema, &json!({"s": {"id": "x"}})));
        assert!(!is_valid(&schema, &json!({"s": {}})));
        assert!(!is_valid(&schema, &json!({"s": null})));
    }

    #[test]
    fn enum_const_pattern() {
        let schema = json!({"enum": ["PASS", "BLOCK"]});
        assert!(is_valid(&schema, &json!("PASS")));
        assert!(!is_valid(&schema, &json!("MAYBE")));
        let c = json!({"const": "stammtisch.pipeline.v0"});
        assert!(is_valid(&c, &json!("stammtisch.pipeline.v0")));
        assert!(!is_valid(&c, &json!("stammtisch.pipeline.v1")));
    }

    #[test]
    fn applicators_allof_oneof_if_then_else() {
        // allOf: every branch must hold.
        let schema = json!({"allOf": [{"type": "integer"}, {"minimum": 2}]});
        assert!(is_valid(&schema, &json!(3)));
        assert!(!is_valid(&schema, &json!(1)));
        assert!(!is_valid(&schema, &json!("x")));

        // oneOf: exactly one branch.
        let schema = json!({"oneOf": [{"type": "null"}, {"type": "string"}]});
        assert!(is_valid(&schema, &json!(null)));
        assert!(is_valid(&schema, &json!("s")));
        assert!(!is_valid(&schema, &json!(7)));
        let ambiguous = json!({"oneOf": [{"type": "integer"}, {"minimum": 0}]});
        assert!(!is_valid(&ambiguous, &json!(7))); // matches both branches

        // if/then/else, including an `if` without `required` (property-
        // present semantics).
        let schema = json!({
            "type": "object",
            "properties": {"op": {"type": "string"}, "detail": {"type": "string"}},
            "if": {"properties": {"op": {"const": "start"}}},
            "then": {"required": ["detail"]},
            "else": {"required": ["op"]}
        });
        assert!(!is_valid(&schema, &json!({"op": "start"}))); // then: detail missing
        assert!(is_valid(&schema, &json!({"op": "start", "detail": "d"})));
        assert!(is_valid(&schema, &json!({"op": "stop"}))); // else: op present
                                                            // `if` passes when the property is absent (properties only constrains
                                                            // present keys), so `then` applies and `detail` is required.
        assert!(!is_valid(&schema, &json!({})));
    }

    #[test]
    fn unique_items() {
        let schema = json!({"uniqueItems": true});
        assert!(is_valid(&schema, &json!(["a", "b"])));
        assert!(!is_valid(&schema, &json!(["a", "a"])));
        // Canonical comparison: key order does not create false uniqueness.
        assert!(!is_valid(
            &schema,
            &json!([{"x": 1, "y": 2}, {"y": 2, "x": 1}])
        ));
        // uniqueItems: false is inert.
        assert!(is_valid(&json!({"uniqueItems": false}), &json!(["a", "a"])));
    }

    #[test]
    fn pipeline_schema_accepts_example() {
        let schema: Value = serde_json::from_str(crate::schemas::PIPELINE).expect("schema parses");
        let good = json!({
            "schema": "stammtisch.pipeline.v0",
            "id": "stock-daily",
            "doctrine": {"pack": "galahad"},
            "stages": [
                {"id": "brief", "product": "doctrine", "out": ["brief.json"]},
                {"id": "deliver", "product": "highball", "in": ["brief.json"],
                 "gate": "packet_authorized", "on_block": "halt"}
            ]
        });
        assert_eq!(violations(&schema, &good), Vec::<String>::new());
        let mut bad = good.clone();
        bad["stages"][0]["product"] = json!("jupyter");
        assert!(!is_valid(&schema, &bad));
        let mut bad2 = good.clone();
        bad2["id"] = json!("Bad ID");
        assert!(!is_valid(&schema, &bad2));
    }
}
