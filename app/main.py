"""
Mutating Admission Webhook: rewrite volume mount paths for certain workloads.

Behavior:
- Target resources: Pod and Deployment objects that have label `nfs-home=true`.
- Mutation: Any container volumeMount `mountPath` that is exactly "/home" or starts
  with "/home/" is rewritten to the equivalent path under "/blah/home".
  Examples:
    /home            -> /blah/home/
    /home/user       -> /blah/home/user

Implementation details:
- Receives AdmissionReview (v1) requests at /mutate (HTTPS).
- Computes RFC 6902 JSON Patch operations and base64-encodes the patch in the
  AdmissionReview response. If no mutation is needed, returns Allowed=true with
  no patch.
- Errors fail-open here; use the webhook failurePolicy to control cluster behavior.

Configuration (optional via env):
- TARGET_LABEL_KEY (default: nfs-home)
- TARGET_LABEL_VALUE (default: true)
- REWRITE_FROM (default: /home)
- REWRITE_TO (default: /blah/home)
"""

import base64
import json
import logging
import os
from typing import Any, Dict, List

from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Static envelope fields needed in AdmissionReview responses
BLANK_ADMISSIONREVIEW = {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview"}

# Configurable behavior via environment variables
LABEL_KEY = os.environ.get("TARGET_LABEL_KEY", "nfs-home")
LABEL_VALUE = os.environ.get("TARGET_LABEL_VALUE", "true")
REWRITE_FROM = os.environ.get("REWRITE_FROM", "/home")
REWRITE_TO = os.environ.get("REWRITE_TO", "/blah/home")


def has_target_label(obj: Dict[str, Any]) -> bool:
    """Return True when the object has the configured label key/value.

    The label is read from metadata.labels on the incoming resource (Pod or the
    top-level Deployment object for admission requests).
    """
    labels = (obj.get("metadata") or {}).get("labels") or {}
    return labels.get(LABEL_KEY) == LABEL_VALUE


def replace_home_path(original_path: str) -> str:
    """Map mount paths from REWRITE_FROM[...] to REWRITE_TO[...].

    - Exact REWRITE_FROM (e.g. "/home") becomes REWRITE_TO with a trailing slash
      (e.g. "/blah/home/") to emphasize directory semantics.
    - Any path starting with REWRITE_FROM + "/" is rewritten to start with
      REWRITE_TO (without double slashes).
    - All other paths are returned unchanged.
    """
    from_exact = REWRITE_FROM
    from_prefix = REWRITE_FROM.rstrip("/") + "/"
    to_base = REWRITE_TO.rstrip("/")

    if original_path == from_exact:
        return to_base + "/"
    if original_path.startswith(from_prefix):
        suffix = original_path[len(from_prefix) - 1 :][len(from_exact) :]
        # The slicing above ensures we preserve everything after the REWRITE_FROM
        # prefix while normalizing slashes at the boundary.
        return to_base + suffix
    return original_path


def patches_for_volume_mounts(base_path: str, containers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build JSON Patch ops to rewrite mountPath entries under a container list.

    Args:
      base_path: JSON Pointer to the container array in the object, e.g.
                 "/spec/containers" or "/spec/template/spec/containers".
      containers: The concrete list of container dicts from the object.

    Returns:
      A list of RFC 6902 JSON Patch operations that replace mountPath values
      when they point to REWRITE_FROM or subpaths beneath it.
    """
    patches: List[Dict[str, Any]] = []
    from_exact = REWRITE_FROM
    from_prefix = REWRITE_FROM.rstrip("/") + "/"

    for container_index, container in enumerate(containers or []):
        volume_mounts = container.get("volumeMounts") or []
        for mount_index, mount in enumerate(volume_mounts):
            mount_path = mount.get("mountPath")
            if not isinstance(mount_path, str):
                continue
            if mount_path == from_exact or mount_path.startswith(from_prefix):
                new_path = replace_home_path(mount_path)
                if new_path != mount_path:
                    patches.append(
                        {
                            "op": "replace",
                            "path": f"{base_path}/{container_index}/volumeMounts/{mount_index}/mountPath",
                            "value": new_path,
                        }
                    )
    return patches


def build_patches(kind: str, obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compute all JSON Patch operations for the given resource kind.

    - For Pod: mutate both spec.containers and spec.initContainers.
    - For Deployment: mutate the pod template at spec.template.spec.*.
    """
    patches: List[Dict[str, Any]] = []

    if kind == "Pod":
        spec = obj.get("spec") or {}
        containers = spec.get("containers") or []
        init_containers = spec.get("initContainers") or []
        patches.extend(patches_for_volume_mounts("/spec/containers", containers))
        patches.extend(patches_for_volume_mounts("/spec/initContainers", init_containers))

    elif kind == "Deployment":
        template_spec = ((obj.get("spec") or {}).get("template") or {}).get("spec") or {}
        containers = template_spec.get("containers") or []
        init_containers = template_spec.get("initContainers") or []
        patches.extend(patches_for_volume_mounts("/spec/template/spec/containers", containers))
        patches.extend(patches_for_volume_mounts("/spec/template/spec/initContainers", init_containers))

    return patches


@app.route("/mutate", methods=["POST"])
def mutate():
    """Admission endpoint that returns a JSON Patch for matching objects.

    Request: AdmissionReview v1 with `request.object` containing the resource.
    Response: AdmissionReview v1 with `response.allowed=true` and optional
              base64-encoded `response.patch` (patchType=JSONPatch).

    This handler filters on kind (Pod/Deployment) and the configured label
    (defaults to `nfs-home=true`). When a mutation is needed, it computes and
    returns the necessary patch ops. On any unexpected error, it fails open
    (allowed=true) so that cluster policy (webhook failurePolicy) governs the
    final behavior.
    """
    try:
        body = request.get_json(force=True, silent=False)
        if not isinstance(body, dict):
            raise ValueError("Invalid AdmissionReview payload")

        req = (body or {}).get("request") or {}
        uid = req.get("uid")
        kind_info = req.get("kind") or {}
        kind = kind_info.get("kind")
        operation = (req.get("operation") or "").upper()
        obj = req.get("object") or {}

        response: Dict[str, Any] = {"uid": uid, "allowed": True}

        if kind in ("Pod", "Deployment") and has_target_label(obj):
            ops = build_patches(kind, obj)
            if ops:
                patch_bytes = json.dumps(ops).encode("utf-8")
                response["patch"] = base64.b64encode(patch_bytes).decode("utf-8")
                response["patchType"] = "JSONPatch"
                try:
                    obj_meta = obj.get("metadata") or {}
                    app.logger.info(
                        "mutation uid=%s kind=%s op=%s ns=%s name=%s patches=%d",
                        uid,
                        kind,
                        operation,
                        obj_meta.get("namespace"),
                        obj_meta.get("name"),
                        len(ops),
                    )
                except Exception:  # noqa: BLE001
                    pass

        return jsonify({**BLANK_ADMISSIONREVIEW, "response": response})

    except Exception as exc:  # noqa: BLE001
        app.logger.exception("mutation failed: %s", exc)
        # On failure, fail-open or fail-closed? We'll fail-open here and rely on FailurePolicy in the webhook config.
        fail_uid = None
        try:
            fail_uid = ((request.get_json(silent=True) or {}).get("request") or {}).get("uid")
        except Exception:  # noqa: BLE001
            pass
        return jsonify({**BLANK_ADMISSIONREVIEW, "response": {"uid": fail_uid, "allowed": True}})


@app.route("/healthz", methods=["GET"])  # liveness/readiness
def healthz():
    """Simple liveness/readiness probe endpoint."""
    return "ok", 200


def main() -> None:
    """Run the Flask app with TLS using cert/key provided via env or defaults."""
    cert_file = os.environ.get("CERT_FILE", "/tls/tls.crt")
    key_file = os.environ.get("KEY_FILE", "/tls/tls.key")
    port = int(os.environ.get("PORT", "8443"))
    app.logger.info(
        "Starting webhook on port %s (label %s=%s, rewrite %s -> %s)",
        port,
        LABEL_KEY,
        LABEL_VALUE,
        REWRITE_FROM,
        REWRITE_TO,
    )
    app.run(host="0.0.0.0", port=port, ssl_context=(cert_file, key_file))


if __name__ == "__main__":
    main()
