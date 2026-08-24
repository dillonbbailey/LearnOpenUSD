// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
//
// three.js viewport: turns the server's world-space geometry payload into a
// scene, handles orbit/pan/zoom, click-to-select, and framing.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const SELECT_COLOR = 0x76b900;

export class Viewport {
  constructor(container, onSelect) {
    this.container = container;
    this.onSelect = onSelect;
    this.selected = null;
    this.wireframe = false;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x101216);

    this.camera = new THREE.PerspectiveCamera(50, 1, 0.01, 100000);
    this.camera.position.set(6, 5, 8);

    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    container.appendChild(this.renderer.domElement);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;

    // `root` carries the up-axis correction so geometry keeps raw USD coords.
    this.root = new THREE.Group();
    this.scene.add(this.root);
    this.contentGroup = new THREE.Group();
    this.root.add(this.contentGroup);

    this.grid = new THREE.GridHelper(20, 20, 0x3a4050, 0x262b36);
    this.scene.add(this.grid);
    this.axes = new THREE.AxesHelper(1.5);
    this.root.add(this.axes);

    this.scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x2a2620, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 1.9);
    key.position.set(5, 10, 7.5);
    this.scene.add(key);
    const fill = new THREE.DirectionalLight(0x99aacc, 0.7);
    fill.position.set(-6, 3, -5);
    this.scene.add(fill);

    this.raycaster = new THREE.Raycaster();
    this._bindPicking();

    this._observer = new ResizeObserver(() => this.resize());
    this._observer.observe(container);
    this.resize();
    this._tick();
  }

  resize() {
    const { clientWidth: w, clientHeight: h } = this.container;
    if (!w || !h) return;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
  }

  _tick = () => {
    requestAnimationFrame(this._tick);
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  };

  _bindPicking() {
    let downAt = null;
    const el = this.renderer.domElement;
    el.addEventListener('pointerdown', (e) => { downAt = { x: e.clientX, y: e.clientY }; });
    el.addEventListener('pointerup', (e) => {
      if (!downAt) return;
      const moved = Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y);
      downAt = null;
      if (moved > 4 || e.button !== 0) return;   // a drag, not a click

      const rect = el.getBoundingClientRect();
      const ndc = new THREE.Vector2(
        ((e.clientX - rect.left) / rect.width) * 2 - 1,
        -((e.clientY - rect.top) / rect.height) * 2 + 1,
      );
      this.raycaster.setFromCamera(ndc, this.camera);
      const hits = this.raycaster.intersectObjects(this.contentGroup.children, true);
      const hit = hits.find((h) => h.object.userData.path);
      this.onSelect(hit ? hit.object.userData.path : null);
    });
  }

  setUpAxis(axis) {
    // three.js is Y-up. Z-up stages get the whole root rotated instead of
    // baking a correction into every prim's matrix.
    this.root.rotation.set(axis === 'Z' ? -Math.PI / 2 : 0, 0, 0);
  }

  setGridVisible(v) { this.grid.visible = v; this.axes.visible = v; }

  setWireframe(v) {
    this.wireframe = v;
    this.contentGroup.traverse((o) => { if (o.material) o.material.wireframe = v; });
  }

  clear() {
    this.contentGroup.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) o.material.dispose();
    });
    this.contentGroup.clear();
  }

  /** Rebuild the whole scene from the server payload. */
  build(items) {
    this.clear();
    const byPath = new Map();

    // Pass 1: everything with real geometry.
    for (const item of items) {
      if (item.kind === 'instancer') continue;
      const geometry = item.kind === 'mesh'
        ? this._meshGeometry(item)
        : this._implicitGeometry(item);
      if (!geometry) continue;
      const mesh = new THREE.Mesh(geometry, this._material(item.color));
      mesh.matrixAutoUpdate = false;
      mesh.matrix.fromArray(item.matrix);   // USD row-major == three column-major
      mesh.userData.path = item.path;
      mesh.userData.baseColor = item.color;
      this.contentGroup.add(mesh);
      byPath.set(item.path, geometry);
    }

    // Pass 2: point instancers, reusing prototype geometry when we drew it.
    for (const item of items) {
      if (item.kind !== 'instancer') continue;
      this._buildInstancer(item, byPath);
    }

    this.setWireframe(this.wireframe);
    this.applySelection(this.selected);
    return this.contentGroup.children.length;
  }

  _material(color) {
    return new THREE.MeshStandardMaterial({
      color: new THREE.Color(...(color || [0.62, 0.64, 0.68])),
      roughness: 0.62, metalness: 0.05,
      side: THREE.DoubleSide, flatShading: false,
    });
  }

  _meshGeometry(item) {
    if (!item.positions?.length || !item.indices?.length) return null;
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(item.positions, 3));
    g.setIndex(item.indices);
    g.computeVertexNormals();
    return g;
  }

  _implicitGeometry(item) {
    switch (item.shape) {
      case 'Sphere':
        return new THREE.SphereGeometry(item.radius, 48, 32);
      case 'Cube':
        return new THREE.BoxGeometry(item.size, item.size, item.size);
      case 'Plane': {
        const g = new THREE.PlaneGeometry(item.width, item.length);
        this._orientAxis(g, item.axis, true);
        return g;
      }
      case 'Cylinder': {
        const g = new THREE.CylinderGeometry(item.radius, item.radius, item.height, 48);
        this._orientAxis(g, item.axis);
        return g;
      }
      case 'Cone': {
        const g = new THREE.ConeGeometry(item.radius, item.height, 48);
        this._orientAxis(g, item.axis);
        return g;
      }
      case 'Capsule': {
        const body = Math.max(item.height - 2 * item.radius, 0.0001);
        const g = new THREE.CapsuleGeometry(item.radius, body, 16, 32);
        this._orientAxis(g, item.axis);
        return g;
      }
      default:
        return null;
    }
  }

  /**
   * three.js builds these primitives Y-aligned; USD's default axis is Z.
   * `planar` geometry (Plane) already faces +Z, so it only needs the X/Y cases.
   */
  _orientAxis(geometry, axis, planar = false) {
    if (axis === 'X') geometry.rotateZ(-Math.PI / 2);
    else if (axis === 'Z' && !planar) geometry.rotateX(Math.PI / 2);
    else if (axis === 'Y' && planar) geometry.rotateX(-Math.PI / 2);
    else if (axis === 'X' && planar) geometry.rotateY(Math.PI / 2);
  }

  _buildInstancer(item, byPath) {
    const count = item.protoIndices.length;
    if (!count) return;
    const stride = 16;
    const perProto = new Map();
    item.protoIndices.forEach((protoIdx, i) => {
      if (!perProto.has(protoIdx)) perProto.set(protoIdx, []);
      perProto.get(protoIdx).push(i);
    });

    const instancerMatrix = new THREE.Matrix4().fromArray(item.matrix);
    for (const [protoIdx, instances] of perProto) {
      const protoPath = item.prototypes[protoIdx];
      const geometry = (protoPath && byPath.get(protoPath))
        || new THREE.BoxGeometry(1, 1, 1);
      const mesh = new THREE.InstancedMesh(
        geometry, this._material(item.color), instances.length);
      const m = new THREE.Matrix4();
      instances.forEach((instanceIndex, slot) => {
        m.fromArray(item.transforms, instanceIndex * stride);
        m.premultiply(instancerMatrix);
        mesh.setMatrixAt(slot, m);
      });
      mesh.instanceMatrix.needsUpdate = true;
      mesh.userData.path = item.path;
      mesh.userData.baseColor = item.color;
      this.contentGroup.add(mesh);
    }
  }

  applySelection(path) {
    this.selected = path;
    this.contentGroup.traverse((o) => {
      if (!o.material || !o.userData.baseColor) return;
      const hit = o.userData.path === path;
      o.material.color.set(hit
        ? SELECT_COLOR
        : new THREE.Color(...o.userData.baseColor));
      o.material.emissive?.set(hit ? 0x1a2600 : 0x000000);
    });
  }

  /** Frame the selected prim, or the whole scene when nothing is selected. */
  frame(path) {
    const targets = [];
    this.contentGroup.traverse((o) => {
      if (!o.isMesh) return;
      if (!path || o.userData.path === path) targets.push(o);
    });
    if (!targets.length) return;

    const box = new THREE.Box3();
    for (const o of targets) box.union(new THREE.Box3().setFromObject(o));
    if (box.isEmpty()) return;

    const size = box.getSize(new THREE.Vector3()).length() || 1;
    const center = box.getCenter(new THREE.Vector3());
    this.root.localToWorld(center);

    const dir = new THREE.Vector3()
      .subVectors(this.camera.position, this.controls.target)
      .normalize();
    this.controls.target.copy(center);
    this.camera.position.copy(center).addScaledVector(dir, size * 1.6 + 0.5);
    this.camera.near = Math.max(size / 1000, 0.001);
    this.camera.far = Math.max(size * 100, 100);
    this.camera.updateProjectionMatrix();
    this.controls.update();
  }
}
