import { loadCv } from "./golden.mjs";
const t = performance.now();
const timer = setTimeout(() => { console.log("still waiting"); process.exit(1); }, 8000);
const cv = await loadCv();
clearTimeout(timer);
console.log("ok", Math.round(performance.now() - t), typeof cv.Mat, typeof cv.findHomography);
