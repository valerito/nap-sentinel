#!/usr/bin/env bash
# Builds dist/nap-sentinel-install.sh (single self-contained file).
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${VERSION:-$(cat VERSION)}"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/nap_sentinel/web" dist
cp nap_sentinel/*.py "$STAGE/nap_sentinel/"
cp nap_sentinel/web/index.html "$STAGE/nap_sentinel/web/"
cp scripts/ensure.sh scripts/uninstall.sh "$STAGE/"
python3 -c "import sys; sys.path.insert(0,'.'); from nap_sentinel.install_hook import BLOCK; open('$STAGE/hook_block.txt','w').write(BLOCK)"
echo "$VERSION" > "$STAGE/VERSION"
tar czf "$STAGE/payload.tgz" --owner=0 --group=0 --sort=name -C "$STAGE" nap_sentinel ensure.sh uninstall.sh hook_block.txt VERSION
SHA="$(sha256sum "$STAGE/payload.tgz" | cut -d' ' -f1)"
base64 -w 76 "$STAGE/payload.tgz" > "$STAGE/payload.b64"
python3 - "$STAGE" "$VERSION" "$SHA" <<'PY'
import sys
stage, version, sha = sys.argv[1:]
t = open("scripts/install.sh.in").read()
t = t.replace("@VERSION@", version).replace("@SHA256@", sha).replace("@PAYLOAD@", open(f"{stage}/payload.b64").read().rstrip("\n"))
open("dist/nap-sentinel-install.sh", "w").write(t)
PY
chmod +x dist/nap-sentinel-install.sh
echo "built dist/nap-sentinel-install.sh ($(du -h dist/nap-sentinel-install.sh | cut -f1), payload sha256 $SHA)"
