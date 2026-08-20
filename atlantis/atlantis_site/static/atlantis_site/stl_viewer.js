import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const loader = new STLLoader();

function initViewer(container) {
    const stlUrl = container.dataset.stlUrl;
    if (!stlUrl) return null;

    const width = container.clientWidth || 400;
    const height = container.clientHeight || 400;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
    camera.position.set(0, 0, 100);

    const renderer = new THREE.WebGLRenderer({ antialias: true , alpha: true,});
    renderer.setClearColor(0x000000, 0);
    renderer.setSize(width, height);
    renderer.setPixelRatio(devicePixelRatio);
    container.appendChild(renderer.domElement);

    scene.add(new THREE.AmbientLight(0xffffff, 0.4));
    const dirLight = new THREE.DirectionalLight(0xffffff, 1.2);
    dirLight.position.set(1, 2, 3);
    scene.add(dirLight);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;

    let mesh = null;
    let disposed = false;

    loader.load(stlUrl, (geometry) => {
        // The viewer can be torn down before the model lands.
        if (disposed) {
            geometry.dispose();
            return;
        }

        geometry.computeBoundingBox();
        geometry.center();

        const material = new THREE.MeshPhongMaterial({ color: 0x4a90d9, specular: 0x222222, shininess: 60 });
        mesh = new THREE.Mesh(geometry, material);
        scene.add(mesh);

        const size = new THREE.Vector3();
        geometry.boundingBox.getSize(size);
        const maxDim = Math.max(size.x, size.y, size.z);
        camera.position.set(0, 0, maxDim * 2);
        controls.update();
    });

    const resize = () => {
        const w = container.clientWidth || 400;
        const h = container.clientHeight || 400;
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        renderer.setSize(w, h);
    };
    window.addEventListener('resize', resize);

    let frame = null;
    function animate() {
        frame = requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();

    // Viewers opened on demand have to give their WebGL context back — browsers
    // only allow a handful at a time.
    function dispose() {
        if (disposed) return;
        disposed = true;
        if (frame !== null) cancelAnimationFrame(frame);
        window.removeEventListener('resize', resize);
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
