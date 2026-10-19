/**
 * Live 3D device model for the /dev orientation widget.
 *
 * Bundled (with three.js and the glTF/Draco loaders tree-shaken in) to
 * app/static/pcat3d.min.js by tools/build_three.sh. The page loads that bundle
 * as a module and drives it through window.PcatModel.
 *
 * The page keeps owning the sensor math: it hands us a finished orientation
 * quaternion every poll and we only render. That split is deliberate -- the
 * guided calibration flow in dev.html (and every R already persisted on a
 * device) is expressed in CSS-3D coordinates, so none of it has to change.
 */

import {
  ACESFilmicToneMapping,
  Box3,
  DirectionalLight,
  Group,
  PerspectiveCamera,
  PMREMGenerator,
  Quaternion,
  Scene,
  SRGBColorSpace,
  Vector3,
  WebGLRenderer,
} from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

// Matches the CAM_TILT the CSS box used, so swapping renderers does not change
// the viewing angle users are used to.
const CAM_TILT_DEG = 22;
const FOV = 35;
// Slerp factor per frame toward the target orientation. The sensor polls at
// ~3 Hz; damping hides that stepping the way the old CSS 0.12s transition did.
const DAMPING = 0.18;
// Below this angle (radians) the model is close enough to its target that we
// stop the rAF loop entirely and idle at zero CPU until the next poll.
const SETTLE_EPSILON = 1e-4;
// Ceiling on the whole glb fetch + Draco decode. Generous: this is a ~350 KiB
// asset over the LAN, so anything slower than this is a failure, not slowness.
const LOAD_TIMEOUT_MS = 20000;

/**
 * CSS-3D quaternion [w,x,y,z] -> three.js Quaternion.
 *
 * The page's frame is CSS 3D: +x right, +y DOWN, +z toward the viewer. three.js
 * has +y UP. The two differ by F = diag(1,-1,1), which is a reflection, not a
 * rotation, so a CSS rotation M becomes F*M*F on this side. Working that
 * through for a quaternion gives (w, x, y, z) -> (w, -x, y, -z).
 */
function cssQuatToThree(q, out) {
  // three.js Quaternion.set takes (x, y, z, w); the page's array is [w,x,y,z].
  return out.set(-q[1], q[2], -q[3], q[0]);
}

class DeviceModel {
  constructor() {
    this.renderer = null;
    this.scene = null;
    this.camera = null;
    this.pivot = null;      // carries the live orientation
    this.inner = null;      // fixed glb -> CSS-rest alignment
    this.canvas = null;
    this.target = new Quaternion();
    this.current = new Quaternion();
    this._raf = null;
    this._observer = null;
    this._disposed = false;
    this._radius = 0.6;
  }

  /**
   * @param {object} opts
   * @param {HTMLCanvasElement} opts.canvas
   * @param {string} opts.modelUrl
   * @param {string} opts.dracoPath directory holding draco_wasm_wrapper.js + .wasm
   * @returns {Promise<void>} resolves once the model is visible, rejects on
   *   WebGL-unavailable or load failure so the caller can fall back.
   */
  async mount(opts) {
    const { canvas, modelUrl, dracoPath } = opts;
    this.canvas = canvas;

    // A missing/blocked WebGL context throws here; let it propagate so the
    // template can show the CSS box instead of an empty white rectangle.
    this.renderer = new WebGLRenderer({
      canvas,
      antialias: true,
      alpha: true,          // let the white card show through
      powerPreference: 'low-power',
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = SRGBColorSpace;
    this.renderer.toneMapping = ACESFilmicToneMapping;
    // The enclosure's top is dark anodised, and "device flat on the desk" is the
    // pose users see most -- at neutral exposure it reads as a black rectangle,
    // so bias bright.
    this.renderer.toneMappingExposure = 1.5;

    this.scene = new Scene();
    this.scene.background = null;

    // The enclosure is brushed aluminium with clearcoat/specular extensions;
    // metals need something to reflect or they render near-black. RoomEnvironment
    // is a procedural studio box, so this costs no extra download.
    const pmrem = new PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = 1.3;
    pmrem.dispose();

    // Two directional lights on top of the environment: a key for a definite
    // highlight that travels as the device turns (pure IBL reads flat), and a
    // front fill so the face-on rest pose is not just a silhouette.
    const key = new DirectionalLight(0xffffff, 1.9);
    key.position.set(1.4, 2.2, 1.8);
    this.scene.add(key);
    const fill = new DirectionalLight(0xffffff, 0.9);
    fill.position.set(-1.6, 0.6, 2.4);
    this.scene.add(fill);

    this.camera = new PerspectiveCamera(FOV, 1, 0.01, 100);

    this.pivot = new Group();
    this.inner = new Group();
    // model_to_glb.py exports +Y up, so the enclosure lies flat in XZ with its
    // top face pointing +Y. The CSS box rested with its top face toward the
    // viewer (+Z), and the calibration quaternions assume that rest pose, so
    // rotate +90 deg about X to reproduce it: +Y -> +Z.
    this.inner.rotation.x = Math.PI / 2;
    this.pivot.add(this.inner);
    this.scene.add(this.pivot);

    const gltf = await this._load(modelUrl, dracoPath);
    const model = gltf.scene;

    // model_to_glb.py --normalize already centres and unit-scales, but re-fit
    // from real bounds so a hand-made or un-normalized glb still frames right.
    const box = new Box3().setFromObject(model);
    const centre = box.getCenter(new Vector3());
    const size = box.getSize(new Vector3());
    const longest = Math.max(size.x, size.y, size.z) || 1;
    // A local matrix is T*R*S, so translation is NOT affected by scale: to get
    // (v - centre)/longest the offset has to be pre-divided. Invisible for our
    // own --normalize'd asset (centre ~0, longest ~1) but wrong for any other.
    model.scale.setScalar(1 / longest);
    model.position.copy(centre).multiplyScalar(-1 / longest);
    // Bounding sphere of the normalized box: the model spins freely, so frame
    // for its worst-case silhouette rather than the flat-on one.
    this._radius = 0.5 * Math.hypot(size.x, size.y, size.z) / longest;

    this.inner.add(model);

    this._resize();
    this._observer = new ResizeObserver(() => this._resize());
    this._observer.observe(canvas.parentElement || canvas);

    this.current.copy(this.target);
    this._renderOnce();
  }

  _load(modelUrl, dracoPath) {
    const draco = new DRACOLoader().setDecoderPath(dracoPath);
    const loader = new GLTFLoader().setDRACOLoader(draco);
    return new Promise((resolve, reject) => {
      // Draco decodes in a Web Worker, and a worker that is blocked outright
      // (a CSP without worker-src blob:, for one) neither succeeds nor errors --
      // loader.load() just never calls back. Without this timeout the widget
      // sits on "Loading..." forever instead of reporting a failure.
      const timer = setTimeout(() => {
        draco.dispose();
        reject(new Error(`timed out loading ${modelUrl} after ${LOAD_TIMEOUT_MS}ms `
                         + `(blocked Draco worker? check the CSP worker-src)`));
      }, LOAD_TIMEOUT_MS);

      loader.load(
        modelUrl,
        (gltf) => { clearTimeout(timer); draco.dispose(); resolve(gltf); },
        undefined,
        (err) => { clearTimeout(timer); draco.dispose(); reject(err); },
      );
    });
  }

  _resize() {
    if (this._disposed || !this.renderer) return;
    const host = this.canvas.parentElement || this.canvas;
    const w = Math.max(1, host.clientWidth | 0);
    const h = Math.max(1, host.clientHeight | 0);
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;

    // Distance that fits the bounding sphere, accounting for the narrower of
    // the two FOVs so the model never clips on a portrait-ish canvas.
    const vFov = (FOV * Math.PI) / 180;
    const hFov = 2 * Math.atan(Math.tan(vFov / 2) * this.camera.aspect);
    const dist = (this._radius * 1.32) / Math.sin(Math.min(vFov, hFov) / 2);

    const tilt = (CAM_TILT_DEG * Math.PI) / 180;
    this.camera.position.set(0, dist * Math.sin(tilt), dist * Math.cos(tilt));
    this.camera.lookAt(0, 0, 0);
    this.camera.updateProjectionMatrix();
    this._renderOnce();
  }

  /** @param {number[]} q CSS-frame quaternion [w,x,y,z] */
  setQuaternionCss(q) {
    if (this._disposed || !Array.isArray(q) || q.length !== 4) return;
    cssQuatToThree(q, this.target).normalize();
    this._start();
  }

  /** Jump to the target without damping (used when the widget first appears). */
  snapToTarget() {
    if (this._disposed) return;
    this.current.copy(this.target);
    this._renderOnce();
  }

  _start() {
    if (this._disposed || this._raf !== null) return;
    const step = () => {
      this._raf = null;
      if (this._disposed) return;
      if (document.hidden) return;   // resumes on the next setQuaternionCss

      this.current.slerp(this.target, DAMPING);
      this._renderOnce();

      if (this.current.angleTo(this.target) > SETTLE_EPSILON) {
        this._raf = requestAnimationFrame(step);
      }
    };
    this._raf = requestAnimationFrame(step);
  }

  _renderOnce() {
    if (this._disposed || !this.renderer) return;
    this.pivot.quaternion.copy(this.current);
    this.renderer.render(this.scene, this.camera);
  }

  dispose() {
    this._disposed = true;
    if (this._raf !== null) cancelAnimationFrame(this._raf);
    if (this._observer) this._observer.disconnect();
    this.scene?.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      const mats = Array.isArray(o.material) ? o.material : (o.material ? [o.material] : []);
      for (const m of mats) m.dispose();
    });
    this.scene?.environment?.dispose?.();
    this.renderer?.dispose();
    this.renderer = null;
  }
}

/**
 * The page loads this bundle with a dynamic import() the first time a g-sensor
 * is actually detected, so hardware without one (the Home model) never pays for
 * the 600 KiB download.
 *
 * @returns {Promise<DeviceModel>}
 */
export async function mount(opts) {
  const m = new DeviceModel();
  await m.mount(opts);
  return m;
}
