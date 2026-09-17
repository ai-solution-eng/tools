#!/usr/bin/env python3
"""Verify a generated EzAppConfig artifact against its values-override.yaml.

Dependency-free (stdlib only). Checks the CR's shape (apiVersion/kind,
labels, required spec fields), that logoImage is valid base64 decoding to a
PNG, that no cluster-populated fields leaked in, and that the spec.values
block matches the edited values-override.yaml exactly.

Usage: verify_ezappconfig.py <ezappconfig.yaml> <values-override.yaml>
"""
import base64
import re
import sys

CLUSTER_FIELDS = ("creationTimestamp", "uid", "resourceVersion", "generation", "finalizers", "managedFields")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    cr_path, values_path = sys.argv[1], sys.argv[2]
    failures = []

    with open(cr_path) as f:
        lines = f.read().splitlines()
    text = "\n".join(lines)

    def has(pattern: str) -> bool:
        return re.search(pattern, text, re.MULTILINE) is not None

    if not has(r"(?m)^apiVersion: ezconfig\.hpe\.ezaf\.com/v1alpha1$"):
        failures.append("apiVersion must be ezconfig.hpe.ezaf.com/v1alpha1")
    if not has(r"(?m)^kind: EzAppConfig$"):
        failures.append("kind must be EzAppConfig")
    if not has(r'(?m)^\s+hpe-ezua/imported-app: "true"$'):
        failures.append('metadata.labels must include hpe-ezua/imported-app: "true"')
    if not has(r"(?m)^  name: \S+"):
        failures.append("metadata.name is missing")
    for field in CLUSTER_FIELDS:
        if has(rf"(?m)^  {field}:"):
            failures.append(f"cluster-populated field must not be in the artifact: {field}")

    for field in (
        "backoffLimit",
        "category",
        "chartVersion",
        "description",
        "install",
        "label",
        "logoImage",
        "name",
        "options",
        "values",
        "version",
    ):
        if not has(rf"(?m)^  {field}:"):
            failures.append(f"spec.{field} is missing")
    if not has(r"(?m)^  category: (dataScience|dataEngineering|analytics)$"):
        failures.append("spec.category must be one of dataScience, dataEngineering, analytics")
    for option in ("create-namespace", "namespace", "timeout", "wait"):
        if not has(rf"(?m)^    {option}:"):
            failures.append(f"spec.options.{option} is missing")

    m = re.search(r"(?m)^  logoImage: (\S+)", text)
    if not m:
        failures.append("spec.logoImage is empty")
    else:
        try:
            decoded = base64.b64decode(m.group(1), validate=True)
            if not decoded.startswith(b"\x89PNG"):
                failures.append("spec.logoImage does not decode to a PNG")
        except Exception:
            failures.append("spec.logoImage is not valid base64")

    values_lines: list[str] = []
    in_block = False
    for line in lines:
        if in_block:
            if line.strip() == "":
                values_lines.append("")
            elif line.startswith("    "):
                values_lines.append(line[4:])
            else:
                in_block = False
        elif line.startswith("  values: |-"):
            in_block = True
    block = "\n".join(values_lines).rstrip("\n")

    with open(values_path) as f:
        expected = f.read().rstrip("\n")
    if not block:
        failures.append("spec.values is empty")
    elif block != expected:
        failures.append("spec.values does not match values-override.yaml")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print("FAILED")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
