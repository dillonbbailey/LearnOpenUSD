# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""USD-side logic for the browser scene editor.

Everything that touches ``pxr`` lives here; ``server.py`` is a thin HTTP shell
over this module. The one long-lived object is :class:`EditorSession`, which
owns a single open ``Usd.Stage`` and mediates every read and edit against it.

Design notes:
    * All geometry is resolved to world space server-side and handed to the
      browser as flat float arrays, so the client never needs USD semantics.
    * Undo is snapshot-based (the root layer serialized to a string before each
      mutation). USD has no native undo stack, and for an editor of this size
      full-layer snapshots are simpler and more predictable than a command log.
    * Traversal uses instance proxies so scenegraph-instanced geometry is
      visible and selectable, matching what usdview shows.
"""

from __future__ import annotations

import io
import contextlib
import math
import traceback
from pathlib import Path
from typing import Any

import numpy as np
from pxr import Usd, UsdGeom, UsdShade, UsdUtils, Sdf, Gf, Tf, Vt

# Prim types we can draw. Anything else still appears in the tree and inspector,
# it just contributes no geometry to the viewport.
_IMPLICIT = {"Sphere", "Cube", "Cylinder", "Cone", "Capsule", "Plane"}

MAX_UNDO = 50


class EditorError(Exception):
    """Raised for user-correctable problems; the server turns these into 400s."""


# --------------------------------------------------------------------------
# value <-> JSON
# --------------------------------------------------------------------------

def to_json(value: Any) -> Any:
    """Convert a USD value into something ``json.dumps`` can handle."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Sdf.AssetPath):
        return value.path
    if isinstance(value, Sdf.Path):
        return str(value)
    if isinstance(value, Gf.Matrix4d):
        return [list(value.GetRow(i)) for i in range(4)]
    if isinstance(value, (Gf.Quatf, Gf.Quatd, Gf.Quath)):
        im = value.GetImaginary()
        return [value.GetReal(), im[0], im[1], im[2]]
    # Gf vectors, Vt arrays, and anything else iterable
    if hasattr(value, "__len__") and not isinstance(value, str):
        return [to_json(v) for v in value]
    return str(value)


def _scalar_from_json(type_name: Sdf.ValueTypeName, value: Any) -> Any:
    """Build one scalar USD value of ``type_name`` from JSON-ish input."""
    cls = type_name.type.pythonClass
    if cls is None:
        # float, int, bool, string, token -- Python natives round-trip directly.
        return value
    if isinstance(value, (list, tuple)):
        flat: list[Any] = []
        for item in value:
            flat.extend(item) if isinstance(item, (list, tuple)) else flat.append(item)
        try:
            return cls(*flat)
        except Exception:
            return cls(flat)
    return cls(value)


def from_json(type_name: Sdf.ValueTypeName, value: Any) -> Any:
    """Inverse of :func:`to_json`, using the attribute's declared type."""
    if type_name.isArray:
        scalar = type_name.scalarType
        array_cls = type_name.type.pythonClass
        items = [_scalar_from_json(scalar, v) for v in (value or [])]
        return array_cls(items) if array_cls is not None else items
    return _scalar_from_json(type_name, value)


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def _triangulate(counts: np.ndarray, indices: np.ndarray) -> np.ndarray:
    """Fan-triangulate arbitrary polygons into a flat triangle index array."""
    tris: list[int] = []
    offset = 0
    for count in counts:
        count = int(count)
        if count >= 3:
            base = indices[offset]
            for i in range(1, count - 1):
                tris.extend((base, indices[offset + i], indices[offset + i + 1]))
        offset += count
    return np.asarray(tris, dtype=np.int32)


def _mesh_arrays(mesh: UsdGeom.Mesh, time: Usd.TimeCode) -> dict | None:
    points = mesh.GetPointsAttr().Get(time)
    counts = mesh.GetFaceVertexCountsAttr().Get(time)
    indices = mesh.GetFaceVertexIndicesAttr().Get(time)
    if not points or not counts or not indices:
        return None

    positions = np.asarray(points, dtype=np.float32)
    tris = _triangulate(np.asarray(counts, dtype=np.int32),
                        np.asarray(indices, dtype=np.int32))
    if tris.size == 0:
        return None

    # Left-handed winding needs flipping for three.js' front-face convention.
    if mesh.GetOrientationAttr().Get(time) == UsdGeom.Tokens.leftHanded:
        tris = tris.reshape(-1, 3)[:, ::-1].reshape(-1)

    return {
        "kind": "mesh",
        "positions": positions.reshape(-1).tolist(),
        "indices": tris.tolist(),
    }


def _implicit_params(prim: Usd.Prim, type_name: str, time: Usd.TimeCode) -> dict:
    """Describe an implicit surface; the client builds the actual geometry."""
    out: dict[str, Any] = {"kind": "implicit", "shape": type_name}
    gprim = UsdGeom.Gprim(prim)
    if type_name == "Sphere":
        out["radius"] = UsdGeom.Sphere(prim).GetRadiusAttr().Get(time) or 1.0
    elif type_name == "Cube":
        out["size"] = UsdGeom.Cube(prim).GetSizeAttr().Get(time) or 2.0
    elif type_name in ("Cylinder", "Cone", "Capsule"):
        schema = getattr(UsdGeom, type_name)(prim)
        out["radius"] = schema.GetRadiusAttr().Get(time) or 1.0
        out["height"] = schema.GetHeightAttr().Get(time) or 2.0
        out["axis"] = str(schema.GetAxisAttr().Get(time) or "Z")
    elif type_name == "Plane":
        plane = UsdGeom.Plane(prim)
        out["width"] = plane.GetWidthAttr().Get(time) or 2.0
        out["length"] = plane.GetLengthAttr().Get(time) or 2.0
        out["axis"] = str(plane.GetAxisAttr().Get(time) or "Z")
    return out


def _display_color(prim: Usd.Prim, time: Usd.TimeCode) -> list[float]:
    gprim = UsdGeom.Gprim(prim)
    if gprim:
        color = gprim.GetDisplayColorAttr().Get(time)
        if color is not None and len(color) > 0:
            return [float(c) for c in color[0]]
    return [0.62, 0.64, 0.68]


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------

class EditorSession:
    """Owns one open stage and every operation the UI can perform on it."""

    def __init__(self) -> None:
        self.stage: Usd.Stage | None = None
        self.path: str | None = None
        self._undo: list[str] = []
        self._redo: list[str] = []

        # Change tracking. `on_change` is a zero-arg callable the transport
        # layer installs to learn that something moved; it is deliberately not
        # a Tornado/websocket detail so this module stays framework-agnostic.
        self.on_change = None
        self._listeners: list = []
        self._revision = 0
        self._pending_resync = False
        self._pending_paths: set[str] = set()

        self.new_stage()

    # -- lifecycle ---------------------------------------------------------

    def _require(self) -> Usd.Stage:
        if self.stage is None:
            raise EditorError("No stage is open.")
        return self.stage

    def new_stage(self) -> None:
        self.stage = Usd.Stage.CreateInMemory("untitled.usda")
        UsdGeom.SetStageUpAxis(self.stage, UsdGeom.Tokens.y)
        UsdGeom.SetStageMetersPerUnit(self.stage, 0.01)
        world = UsdGeom.Xform.Define(self.stage, "/World")
        self.stage.SetDefaultPrim(world.GetPrim())
        self.path = None
        self._undo.clear()
        self._redo.clear()
        self._watch_stage()

    def open(self, file_path: str) -> None:
        resolved = Path(file_path).expanduser().resolve()
        if not resolved.exists():
            raise EditorError(f"File not found: {resolved}")
        stage = Usd.Stage.Open(str(resolved))
        if stage is None:
            raise EditorError(f"USD could not open: {resolved}")
        self.stage = stage
        self.path = str(resolved)
        self._undo.clear()
        self._redo.clear()
        self._watch_stage()

    def save(self, file_path: str | None = None) -> str:
        stage = self._require()
        target = file_path or self.path
        if not target:
            raise EditorError("No save path given and this stage is unsaved.")
        resolved = Path(target).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if self.path and not file_path:
            stage.GetRootLayer().Save()
        else:
            stage.GetRootLayer().Export(str(resolved))
            # Reopen so later saves write to the file rather than memory, but
            # keep the undo stack -- snapshots are plain USDA text and stay
            # valid across the swap. Losing history on Save As is surprising.
            undo, redo = list(self._undo), list(self._redo)
            self.open(str(resolved))
            self._undo, self._redo = undo, redo
        self.path = str(resolved)
        return self.path

    # -- change notification -----------------------------------------------

    def _watch_stage(self) -> None:
        """(Re)subscribe to USD change notices for the current stage.

        Called on every stage swap. Listener keys must be held, or Tf revokes
        the subscription when they are garbage collected.
        """
        self._revoke()
        if self.stage is None:
            return
        self._listeners = [
            Tf.Notice.Register(Usd.Notice.ObjectsChanged,
                               self._on_objects_changed, self.stage),
            Tf.Notice.Register(Usd.Notice.StageEditTargetChanged,
                               self._on_edit_target_changed, self.stage),
        ]
        # A new stage is itself a change: force a full client rebuild.
        self._note_change(resync=True, paths=["/"])

    def _revoke(self) -> None:
        for key in self._listeners:
            key.Revoke()
        self._listeners = []

    def _note_change(self, resync: bool, paths) -> None:
        self._revision += 1
        self._pending_resync = self._pending_resync or resync
        self._pending_paths.update(paths)
        if self.on_change is not None:
            self.on_change()

    def _on_objects_changed(self, notice, sender) -> None:
        """USD's primary change notice.

        Resynced paths mean the composed structure changed (prims added or
        removed, arcs or variants edited) and the client must rebuild the tree.
        Info-only paths mean an attribute value moved -- the tree is still
        valid, but geometry may need redrawing (points, radius, xformOp, ...).
        """
        resynced = [str(p) for p in notice.GetResyncedPaths()]
        info_only = [str(p) for p in notice.GetChangedInfoOnlyPaths()]
        if not resynced and not info_only:
            return
        self._note_change(resync=bool(resynced), paths=resynced + info_only)

    def _on_edit_target_changed(self, notice, sender) -> None:
        self._note_change(resync=False, paths=[])

    def drain_change(self) -> dict | None:
        """Take the coalesced change record, or None if nothing is pending.

        A single user edit can emit many notices; the transport calls this once
        after a short debounce so the browser gets one redraw, not dozens.
        """
        if not self._pending_paths and not self._pending_resync:
            return None
        record = {
            "revision": self._revision,
            "resync": self._pending_resync,
            # Cap the path list: a stage-wide resync can name thousands, and
            # the client only needs them to decide whether to refetch detail.
            "paths": sorted(self._pending_paths)[:200],
            "truncated": len(self._pending_paths) > 200,
        }
        self._pending_resync = False
        self._pending_paths.clear()
        return record

    # -- undo --------------------------------------------------------------

    def checkpoint(self) -> None:
        """Snapshot the root layer. Call before any mutation."""
        stage = self._require()
        self._undo.append(stage.GetRootLayer().ExportToString())
        del self._undo[:-MAX_UNDO]
        self._redo.clear()

    def _restore(self, source: list[str], sink: list[str]) -> None:
        stage = self._require()
        if not source:
            raise EditorError("Nothing to undo." if source is self._undo
                              else "Nothing to redo.")
        sink.append(stage.GetRootLayer().ExportToString())
        stage.GetRootLayer().ImportFromString(source.pop())

    def undo(self) -> None:
        self._restore(self._undo, self._redo)

    def redo(self) -> None:
        self._restore(self._redo, self._undo)

    # -- read --------------------------------------------------------------

    def stage_info(self) -> dict:
        stage = self._require()
        root = stage.GetRootLayer()
        default_prim = stage.GetDefaultPrim()
        return {
            "path": self.path,
            "identifier": root.identifier,
            "dirty": root.dirty,
            "defaultPrim": default_prim.GetName() if default_prim else None,
            "upAxis": str(UsdGeom.GetStageUpAxis(stage)),
            "metersPerUnit": UsdGeom.GetStageMetersPerUnit(stage),
            "startTimeCode": stage.GetStartTimeCode(),
            "endTimeCode": stage.GetEndTimeCode(),
            "canUndo": bool(self._undo),
            "canRedo": bool(self._redo),
            "editTarget": stage.GetEditTarget().GetLayer().identifier,
            # Lets the client ignore pushed notices for edits it already applied.
            "revision": self._revision,
            "layers": [
                {"identifier": layer.identifier,
                 "display": Path(layer.identifier).name or layer.identifier,
                 "anonymous": layer.anonymous,
                 "dirty": layer.dirty}
                for layer in stage.GetLayerStack()
            ],
        }

    def scenegraph(self) -> dict:
        stage = self._require()

        def build(prim: Usd.Prim) -> dict:
            # Full variant state travels with the tree so the right-click menu
            # can be built instantly; only prims that have variant sets pay for it.
            variant_sets = []
            if prim.IsValid() and not prim.IsPseudoRoot():
                for set_name in prim.GetVariantSets().GetNames():
                    vset = prim.GetVariantSet(set_name)
                    variant_sets.append({
                        "name": set_name,
                        "variants": list(vset.GetVariantNames()),
                        "selection": vset.GetVariantSelection(),
                    })
            node = {
                "path": str(prim.GetPath()),
                "name": prim.GetName(),
                "type": prim.GetTypeName() or "",
                "active": prim.IsActive(),
                "visible": self._is_visible(prim),
                "instanceable": prim.IsInstanceable(),
                "isInstanceProxy": prim.IsInstanceProxy(),
                "kind": Usd.ModelAPI(prim).GetKind() or "",
                "variantSets": variant_sets,
                "arcs": self._arc_summary(prim),
                "children": [],
            }
            children = prim.GetFilteredChildren(
                Usd.TraverseInstanceProxies(Usd.PrimAllPrimsPredicate))
            node["children"] = [build(child) for child in children]
            return node

        root = build(stage.GetPseudoRoot())
        root["name"] = "/"
        return root

    @staticmethod
    def _is_visible(prim: Usd.Prim) -> bool:
        imageable = UsdGeom.Imageable(prim)
        if not imageable:
            return True
        return imageable.ComputeVisibility() != UsdGeom.Tokens.invisible

    @staticmethod
    def _arc_summary(prim: Usd.Prim) -> list[str]:
        """Which composition arcs are authored directly on this prim."""
        if not prim.IsValid() or prim.IsPseudoRoot():
            return []
        arcs = []
        if prim.HasAuthoredReferences():
            arcs.append("reference")
        if prim.HasAuthoredPayloads():
            arcs.append("payload")
        if prim.HasAuthoredInherits():
            arcs.append("inherit")
        if prim.HasAuthoredSpecializes():
            arcs.append("specialize")
        if prim.HasVariantSets():
            arcs.append("variant")
        return arcs

    def geometry(self) -> dict:
        """World-space drawable geometry for every visible imageable prim."""
        stage = self._require()
        time = Usd.TimeCode.Default()
        xform_cache = UsdGeom.XformCache(time)
        items: list[dict] = []

        predicate = Usd.TraverseInstanceProxies(
            Usd.PrimIsActive & Usd.PrimIsDefined & Usd.PrimIsLoaded)
        for prim in Usd.PrimRange(stage.GetPseudoRoot(), predicate):
            type_name = prim.GetTypeName()
            if not type_name or not self._is_visible(prim):
                continue

            purpose = UsdGeom.Imageable(prim).ComputePurpose() if UsdGeom.Imageable(prim) else None
            if purpose in (UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy):
                continue

            if type_name == "Mesh":
                payload = _mesh_arrays(UsdGeom.Mesh(prim), time)
            elif type_name in _IMPLICIT:
                payload = _implicit_params(prim, type_name, time)
            elif type_name == "PointInstancer":
                payload = self._point_instancer(prim, time)
            else:
                payload = None

            if payload is None:
                continue

            matrix = xform_cache.GetLocalToWorldTransform(prim)
            payload.update({
                "path": str(prim.GetPath()),
                "matrix": [c for row in range(4) for c in matrix.GetRow(row)],
                "color": _display_color(prim, time),
            })
            items.append(payload)

        return {"items": items}

    @staticmethod
    def _point_instancer(prim: Usd.Prim, time: Usd.TimeCode) -> dict | None:
        """Emit per-instance transforms; prototypes are drawn from their own prims."""
        instancer = UsdGeom.PointInstancer(prim)
        transforms = instancer.ComputeInstanceTransformsAtTime(time, time)
        proto_indices = instancer.GetProtoIndicesAttr().Get(time)
        targets = instancer.GetPrototypesRel().GetTargets()
        if transforms is None or proto_indices is None or not targets:
            return None
        return {
            "kind": "instancer",
            "prototypes": [str(t) for t in targets],
            "protoIndices": [int(i) for i in proto_indices],
            "transforms": [c for m in transforms for row in range(4) for c in m.GetRow(row)],
        }

    def prim_detail(self, prim_path: str) -> dict:
        stage = self._require()
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            raise EditorError(f"No prim at {prim_path}")

        attributes = []
        for attr in sorted(prim.GetAttributes(), key=lambda a: a.GetName()):
            try:
                value = to_json(attr.Get())
            except Exception as exc:  # unresolvable or exotic value types
                value = f"<unreadable: {exc}>"
            attributes.append({
                "name": attr.GetName(),
                "typeName": str(attr.GetTypeName()),
                "value": value,
                "authored": attr.HasAuthoredValue(),
                "custom": attr.IsCustom(),
                "variability": str(attr.GetVariability()),
                "editable": self._is_editable(attr),
                "connections": [str(p) for p in attr.GetConnections()],
                "timeSamples": attr.GetNumTimeSamples(),
            })

        relationships = [
            {"name": rel.GetName(), "targets": [str(t) for t in rel.GetTargets()]}
            for rel in sorted(prim.GetRelationships(), key=lambda r: r.GetName())
        ]

        return {
            "path": str(prim.GetPath()),
            "name": prim.GetName(),
            "type": prim.GetTypeName() or "",
            "documentation": prim.GetDocumentation(),
            "active": prim.IsActive(),
            "instanceable": prim.IsInstanceable(),
            "isInstanceProxy": prim.IsInstanceProxy(),
            "kind": Usd.ModelAPI(prim).GetKind() or "",
            # "Sdf.SpecifierDef" -> "def", matching how it reads in USDA.
            "specifier": str(prim.GetSpecifier()).split(".")[-1]
                         .replace("Specifier", "").lower(),
            "appliedSchemas": list(prim.GetAppliedSchemas()),
            "transform": self._transform_values(prim),
            "defaultPrim": stage.GetDefaultPrim() == prim,
            "attributes": attributes,
            "relationships": relationships,
            "composition": self.composition_detail(prim),
        }

    @staticmethod
    def _is_editable(attr: Usd.Attribute) -> bool:
        """Only offer inline editing for types the UI knows how to round-trip."""
        type_name = attr.GetTypeName()
        if type_name.isArray:
            return False
        return str(type_name) in {
            "float", "double", "half", "int", "int64", "uint", "bool",
            "string", "token", "asset",
            "float2", "float3", "float4", "double2", "double3", "double4",
            "int2", "int3", "int4", "color3f", "color4f", "normal3f",
            "point3f", "vector3f", "texCoord2f",
        }

    @staticmethod
    def _transform_values(prim: Usd.Prim) -> dict | None:
        """Translate/rotate/scale via XformCommonAPI, when the prim supports it."""
        common = UsdGeom.XformCommonAPI(prim)
        if not common:
            return None
        translate, rotate, scale, pivot, rotation_order = common.GetXformVectors(
            Usd.TimeCode.Default())
        return {
            "translate": [float(v) for v in translate],
            "rotate": [float(v) for v in rotate],
            "scale": [float(v) for v in scale],
            "pivot": [float(v) for v in pivot],
            # "UsdGeom.XformCommonAPI.RotationOrderXYZ" -> "XYZ"
            "rotationOrder": str(rotation_order).split(".")[-1].replace("RotationOrder", ""),
        }

    def composition_detail(self, prim: Usd.Prim) -> dict:
        """The opinion stack plus variant state -- the 'why does it look like this' pane."""
        specs = []
        for spec in prim.GetPrimStack():
            layer = spec.layer
            specs.append({
                "layer": Path(layer.identifier).name or layer.identifier,
                "identifier": layer.identifier,
                "path": str(spec.path),
                "specifier": str(spec.specifier).split(".")[-1],
                "hasReferences": bool(spec.referenceList.GetAppliedItems()),
                "hasPayloads": bool(spec.payloadList.GetAppliedItems()),
            })

        variant_sets = []
        for name in prim.GetVariantSets().GetNames():
            vset = prim.GetVariantSet(name)
            variant_sets.append({
                "name": name,
                "variants": list(vset.GetVariantNames()),
                "selection": vset.GetVariantSelection(),
            })

        references, payloads = [], []
        for spec in prim.GetPrimStack():
            for ref in spec.referenceList.GetAppliedItems():
                references.append({"assetPath": ref.assetPath,
                                   "primPath": str(ref.primPath),
                                   "layer": Path(spec.layer.identifier).name})
            for pay in spec.payloadList.GetAppliedItems():
                payloads.append({"assetPath": pay.assetPath,
                                 "primPath": str(pay.primPath),
                                 "layer": Path(spec.layer.identifier).name})

        return {
            "primStack": specs,
            "variantSets": variant_sets,
            "references": references,
            "payloads": payloads,
        }

    def attribute_stack(self, prim_path: str, attr_name: str) -> dict:
        """Per-layer opinions for one attribute, strongest first."""
        stage = self._require()
        prim = stage.GetPrimAtPath(prim_path)
        if not prim:
            raise EditorError(f"No prim at {prim_path}")
        attr = prim.GetAttribute(attr_name)
        if not attr:
            raise EditorError(f"No attribute {attr_name} on {prim_path}")
        entries = []
        for spec in attr.GetPropertyStack(Usd.TimeCode.Default()):
            entries.append({
                "layer": Path(spec.layer.identifier).name or spec.layer.identifier,
                "identifier": spec.layer.identifier,
                "value": to_json(spec.default),
                "path": str(spec.path),
            })
        return {"attribute": attr_name, "opinions": entries}

    def usda(self, mode: str = "root", identifier: str | None = None) -> str:
        stage = self._require()
        if mode == "flattened":
            return stage.Flatten().ExportToString()
        if mode == "layer" and identifier:
            layer = Sdf.Layer.Find(identifier)
            if layer is None:
                raise EditorError(f"Layer not found: {identifier}")
            return layer.ExportToString()
        return stage.GetRootLayer().ExportToString()

    # -- write -------------------------------------------------------------

    def create_prim(self, parent: str, name: str, type_name: str) -> str:
        stage = self._require()
        if not Sdf.Path.IsValidIdentifier(name):
            raise EditorError(f"{name!r} is not a valid prim name.")
        parent_path = Sdf.Path(parent or "/")
        target = parent_path.AppendChild(name)
        suffix = 1
        while stage.GetPrimAtPath(target):
            suffix += 1
            target = parent_path.AppendChild(f"{name}{suffix}")
        self.checkpoint()
        stage.DefinePrim(target, type_name or "")
        return str(target)

    def delete_prim(self, prim_path: str) -> None:
        stage = self._require()
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or prim.IsPseudoRoot():
            raise EditorError(f"Cannot delete {prim_path}")
        self.checkpoint()
        if not stage.RemovePrim(prim_path):
            raise EditorError(
                f"Could not remove {prim_path}. It is likely defined in a layer "
                "that is not the current edit target.")

    def rename_prim(self, prim_path: str, new_name: str) -> str:
        stage = self._require()
        if not Sdf.Path.IsValidIdentifier(new_name):
            raise EditorError(f"{new_name!r} is not a valid prim name.")
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or prim.IsPseudoRoot():
            raise EditorError(f"Cannot rename {prim_path}")
        self.checkpoint()
        layer = stage.GetEditTarget().GetLayer()
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(Sdf.NamespaceEdit.Rename(Sdf.Path(prim_path), new_name))
        if not layer.Apply(edit):
            raise EditorError(
                f"Rename failed. {prim_path} may not be defined in the current "
                "edit target layer.")
        return str(Sdf.Path(prim_path).GetParentPath().AppendChild(new_name))

    def set_transform(self, prim_path: str, translate=None, rotate=None,
                      scale=None) -> None:
        stage = self._require()
        prim = stage.GetPrimAtPath(prim_path)
        if not prim:
            raise EditorError(f"No prim at {prim_path}")
        common = UsdGeom.XformCommonAPI(prim)
        if not common:
            raise EditorError(f"{prim_path} is not transformable.")
        self.checkpoint()
        if translate is not None:
            common.SetTranslate(Gf.Vec3d(*[float(v) for v in translate]))
        if rotate is not None:
            common.SetRotate(Gf.Vec3f(*[float(v) for v in rotate]))
        if scale is not None:
            common.SetScale(Gf.Vec3f(*[float(v) for v in scale]))

    def set_attribute(self, prim_path: str, attr_name: str, value: Any) -> None:
        stage = self._require()
        prim = stage.GetPrimAtPath(prim_path)
        if not prim:
            raise EditorError(f"No prim at {prim_path}")
        attr = prim.GetAttribute(attr_name)
        if not attr:
            raise EditorError(f"No attribute {attr_name} on {prim_path}")
        self.checkpoint()
        try:
            attr.Set(from_json(attr.GetTypeName(), value))
        except Exception as exc:
            self.undo()
            raise EditorError(f"Could not set {attr_name}: {exc}") from exc

    def set_metadata(self, prim_path: str, key: str, value: Any) -> None:
        stage = self._require()
        prim = stage.GetPrimAtPath(prim_path)
        if not prim:
            raise EditorError(f"No prim at {prim_path}")
        self.checkpoint()
        if key == "active":
            prim.SetActive(bool(value))
        elif key == "instanceable":
            prim.SetInstanceable(bool(value))
        elif key == "kind":
            Usd.ModelAPI(prim).SetKind(str(value))
        elif key == "documentation":
            prim.SetDocumentation(str(value))
        elif key == "typeName":
            prim.SetTypeName(str(value))
        else:
            raise EditorError(f"Unsupported metadata key: {key}")

    def set_default_prim(self, prim_path: str) -> None:
        stage = self._require()
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or prim.GetPath().GetPathElementCount() != 1:
            raise EditorError("The default prim must be a top-level prim.")
        self.checkpoint()
        stage.SetDefaultPrim(prim)

    # -- composition -------------------------------------------------------

    def add_arc(self, prim_path: str, asset_path: str, arc: str,
                target_prim: str = "") -> None:
        stage = self._require()
        prim = stage.GetPrimAtPath(prim_path)
        if not prim:
            raise EditorError(f"No prim at {prim_path}")
        self.checkpoint()
        prim_target = Sdf.Path(target_prim) if target_prim else Sdf.Path()
        if arc == "reference":
            prim.GetReferences().AddReference(asset_path, prim_target)
        elif arc == "payload":
            prim.GetPayloads().AddPayload(asset_path, prim_target)
        elif arc == "inherit":
            prim.GetInherits().AddInherit(Sdf.Path(target_prim or asset_path))
        elif arc == "specialize":
            prim.GetSpecializes().AddSpecialize(Sdf.Path(target_prim or asset_path))
        else:
            raise EditorError(f"Unknown arc type: {arc}")

    def add_sublayer(self, asset_path: str) -> None:
        stage = self._require()
        self.checkpoint()
        root = stage.GetRootLayer()
        if asset_path in root.subLayerPaths:
            raise EditorError(f"{asset_path} is already a sublayer.")
        root.subLayerPaths.insert(0, asset_path)

    def add_variant_set(self, prim_path: str, set_name: str,
                        variant_names: list[str]) -> None:
        stage = self._require()
        prim = stage.GetPrimAtPath(prim_path)
        if not prim:
            raise EditorError(f"No prim at {prim_path}")
        self.checkpoint()
        vset = prim.GetVariantSets().AddVariantSet(set_name)
        for name in variant_names:
            vset.AddVariant(name)
        if variant_names:
            vset.SetVariantSelection(variant_names[0])

    def set_variant(self, prim_path: str, set_name: str, variant: str) -> None:
        stage = self._require()
        prim = stage.GetPrimAtPath(prim_path)
        if not prim:
            raise EditorError(f"No prim at {prim_path}")
        vset = prim.GetVariantSet(set_name)
        if not vset:
            raise EditorError(f"No variant set {set_name} on {prim_path}")
        self.checkpoint()
        vset.SetVariantSelection(variant)

    def set_edit_target(self, identifier: str) -> None:
        stage = self._require()
        layer = Sdf.Layer.Find(identifier)
        if layer is None:
            raise EditorError(f"Layer not found: {identifier}")
        stage.SetEditTarget(Usd.EditTarget(layer))

    # -- python console ----------------------------------------------------

    def run_python(self, code: str, namespace: dict) -> dict:
        """Execute ``code`` against the live stage, capturing stdout/stderr.

        The namespace persists across calls so the console behaves like a REPL.
        A checkpoint is taken first, so a script that mangles the stage is undoable.
        """
        stage = self._require()
        self.checkpoint()
        namespace.update({
            "stage": stage, "Usd": Usd, "UsdGeom": UsdGeom, "UsdShade": UsdShade,
            "UsdUtils": UsdUtils, "Sdf": Sdf, "Gf": Gf, "Tf": Tf, "Vt": Vt,
            "session": self,
        })
        buffer = io.StringIO()
        result: Any = None
        error: str | None = None
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                try:
                    # Prefer eval so the console echoes expression results.
                    result = eval(compile(code, "<console>", "eval"), namespace)
                except SyntaxError:
                    exec(compile(code, "<console>", "exec"), namespace)
        except Exception:
            error = traceback.format_exc(limit=3)
        return {
            "stdout": buffer.getvalue(),
            "result": None if result is None else repr(result),
            "error": error,
        }
