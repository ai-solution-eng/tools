"""Regenerate the PCAI import artifact from the chart.

Values source: helm/local/values.g2.yaml when it exists (the site/demo file),
otherwise helm/values.yaml (chart defaults). Run it from anywhere:

    python3 ezapp/make_ezappconfig.py
"""
import base64, time, yaml, pathlib


def _png_alpha(data):
    """(color type, has tRNS) — color type 4/6 or a tRNS chunk means alpha."""
    i, trns = 8, False
    while i + 8 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        if kind == b"tRNS":
            trns = True
        if kind == b"IDAT":
            break
        i += 12 + length
    return data[25], trns


root = pathlib.Path(__file__).resolve().parent.parent
chart = yaml.safe_load((root / "helm/Chart.yaml").read_text())
_site = root / "helm/local/values.g2.yaml"
values_path = _site if _site.exists() else root / "helm/values.yaml"
values = values_path.read_text()
print("values source:", values_path.relative_to(root))
# App icon: ezapp/logo.png — the architecture diagram (hicache-arch-simple.png,
# white-backed) fitted to 224px wide and centred on a 256x256 white canvas. PCAI
# paints the icon on a dark card, so it must be opaque: a transparent PNG lets the
# card show through and the icon reads as broken. Re-derive it if the diagram
# changes. The blue HPE icon is kept as ezapp/logo-blue.png if it is ever wanted back.
logo_bytes = (root / "ezapp/logo.png").read_bytes()
assert logo_bytes[:8] == b"\x89PNG\r\n\x1a\n", "ezapp/logo.png is not a PNG"
_color_type, _has_trns = _png_alpha(logo_bytes)
assert _color_type not in (4, 6) and not _has_trns, (
    "ezapp/logo.png has an alpha channel; flatten it onto white first: "
    "python3 -c \"from PIL import Image; im = Image.open('ezapp/logo.png').convert('RGBA'); "
    "Image.alpha_composite(Image.new('RGBA', im.size, (255, 255, 255, 255)), im)"
    ".convert('RGB').save('ezapp/logo.png')\""
)
logo = base64.b64encode(logo_bytes).decode()
name = chart["name"]; version = chart["version"]
epoch_ms = int(time.time() * 1000)
desc = "Shared KV-cache store for model deployments: one Mooncake pool, many replicas, with a live statistics dashboard."
indented = "\n".join(("    " + line) if line.strip() else "" for line in values.splitlines())
cr = f"""apiVersion: ezconfig.hpe.ezaf.com/v1alpha1
kind: EzAppConfig
metadata:
  labels:
    hpe-ezua/imported-app: "true"
  name: {name}-{version}-{epoch_ms}
spec:
  backoffLimit: 3
  category: dataScience
  chartVersion: "{version}"
  description: "{desc}"
  install: true
  label: "Mooncake"
  logoImage: {logo}
  name: "{name}"
  options:
    create-namespace: "true"
    namespace: "mooncake"
    timeout: 45m
    wait: "true"
  values: |-
{indented}
  version: ""
"""
(root / "ezapp/values-override.yaml").write_text(values)
(root / "ezapp/ezappconfig-mooncake.yaml").write_text(cr)
doc = yaml.safe_load(cr)
assert doc["spec"]["values"].rstrip("\n") == values.rstrip("\n"), "values block mismatch"
assert doc["spec"]["name"] == name and doc["spec"]["chartVersion"] == version
raw = base64.b64decode(doc["spec"]["logoImage"])
assert raw[:8] == b"\x89PNG\r\n\x1a\n", "logo is not a PNG"
print("OK", doc["metadata"]["name"], "values lines", len(values.splitlines()), "logo bytes", len(raw))