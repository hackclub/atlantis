import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const loader = new STLLoader();

function initViewer(container) {
    const stlUrl = container.dataset.stlUrl;
    if (!stlUrl) return null;

    const scene = new THREE.Scene();
    // Sized for real once the container has a box to measure — on the review
    // pages it has none yet, so nothing here may depend on its dimensions.
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);
    camera.position.set(0, 0, 100);

    const renderer = new THREE.WebGLRenderer({ antialias: true , alpha: true,});
    renderer.setClearColor(0x000000, 0);
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    container.appendChild(renderer.domElement);
    renderer.domElement.style.display = 'block';

    scene.add(new THREE.AmbientLight(0xffffff, 0.4));
    const dirLight = new THREE.DirectionalLight(0xffffff, 1.2);
    dirLight.position.set(1, 2, 3);
    scene.add(dirLight);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;

    let mesh = null;
    let disposed = false;
    let radius = 0;
    let sized = false;
    // Auto-fit follows the box until the model is grabbed; after that the view
    // is the reviewer's, and a reflow must not yank it back.
    let touched = false;
    controls.addEventListener('start', () => { touched = true; });

    // Pull the camera back far enough that the model's bounding sphere clears
    // whichever fov is tighter, so a short wide box crops no more than a square
    // one. Near/far ride the same distance — a fixed far plane cuts through
    // anything modelled at a larger scale.
    function frame() {
        if (!radius || !sized) return;
        const vFov = THREE.MathUtils.degToRad(camera.fov);
        const hFov = 2 * Math.atan(Math.tan(vFov / 2) * camera.aspect);
        const distance = (radius / Math.sin(Math.min(vFov, hFov) / 2)) * 1.15;
        camera.near = Math.max(distance / 1000, 0.001);
        camera.far = distance + radius * 10;
        camera.position.set(0, 0, distance);
        controls.target.set(0, 0, 0);
        camera.updateProjectionMatrix();
        controls.update();
    }

    // The entry an STL sits in is usually collapsed when the page loads, so the
    // first honest measurement arrives from the observer, not from init.
    function measure() {
        const w = container.clientWidth;
        const h = container.clientHeight;
        if (!w || !h) return;
        renderer.setSize(w, h);
        camera.aspect = w / h;
        sized = true;
        if (touched) camera.updateProjectionMatrix();
        else frame();
    }

    const observer = new ResizeObserver(measure);
    observer.observe(container);
    measure();

    loader.load(stlUrl, (geometry) => {
        // The viewer can be torn down before the model lands.
        if (disposed) {
            geometry.dispose();
            return;
        }

        geometry.center();
        geometry.computeBoundingSphere();

        const material = new THREE.MeshPhongMaterial({ color: 0x4a90d9, specular: 0x222222, shininess: 60 });
        mesh = new THREE.Mesh(geometry, material);
        scene.add(mesh);

        radius = geometry.boundingSphere.radius || 1;
        frame();
    });

    let frameId = null;
    function animate() {
        frameId = requestAnimationFrame(animate);
        if (!sized) return;
        controls.update();
        renderer.render(scene, camera);
    }
    animate();

    // Viewers opened on demand have to give their WebGL context back — browsers
    // only allow a handful at a time.
    function dispose() {
        if (disposed) return;
        disposed = true;
        if (frameId !== null) cancelAnimationFrame(frameId);
        observer.disconnect();
        controls.dispose();
        if (mesh) {
            scene.remove(mesh);
            mesh.geometry.dispose();
            mesh.material.dispose();
        }
        renderer.dispose();
        renderer.forceContextLoss();
        renderer.domElement.remove();
    }

    return { dispose };
}

// Anything rendered on demand (the project book's close-ups) mounts its own.
window.AtlantisSTL = { init: initViewer };
document.dispatchEvent(new CustomEvent('atlantis:stl-ready'));

document.querySelectorAll('.stl-viewer').forEach(initViewer);
