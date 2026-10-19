/** One renderer for the live camera overlay and the projector. No saved outlines. */
import { containTransform, PALETTE_ROLES, projectorColors } from "./ui-core.mjs";

export const LIVE_MAX_AGE_MS = 1500;
export const FALLBACK_COLORS = { start: "#ffdf53", goal: "#ffa659", bumper: "#77ec8f", marker: "#ff77d3" };
const positive = (n) => Number.isFinite(n) && n > 0;
const validPoint = (p) => p && Number.isFinite(p.x) && Number.isFinite(p.y);

function fitFrame(frameWidth, frameHeight, width, height) {
  return [frameWidth, frameHeight, width, height].every(positive)
    ? containTransform(width, height, frameWidth, frameHeight) : null;
}

export function drawLiveDetection(ctx, preview, palette, width, height, theme = "dark") {
  const fit = fitFrame(preview?.width, preview?.height, width, height);
  if (!fit) return;
  for (const role of PALETTE_ROLES) {
    const notes = preview.roles?.[role]?.notes;
    if (!Array.isArray(notes)) continue;
    const color = palette?.roles?.[role]?.hex || FALLBACK_COLORS[role];
    for (const shape of notes) {
      if (!Array.isArray(shape?.points) || shape.points.length < 3 || !shape.points.every(validPoint)) continue;
      ctx.save();
      ctx.beginPath();
      shape.points.forEach((point, index) => {
        const x = fit.offsetX + point.x * fit.scale;
        const y = fit.offsetY + point.y * fit.scale;
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.closePath();
      ctx.shadowColor = color;
      ctx.shadowBlur = 18;
      ctx.globalAlpha = .3;
      ctx.fillStyle = color;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#ffffff";
      ctx.stroke();
      ctx.restore();
    }
  }
}

/** Camera-pixel game geometry shares the preview's contain transform. */
export function drawLiveGame(ctx, game, frameWidth, frameHeight, width, height, theme = "dark") {
  const fit = fitFrame(game?.width || frameWidth, game?.height || frameHeight, width, height);
  if (!fit || !validPoint(game?.position)) return;
  const colors = projectorColors(theme);
  const ball = { x: fit.offsetX + game.position.x * fit.scale, y: fit.offsetY + game.position.y * fit.scale };
  ctx.save();
  if (game.status === "ready") {
    const angle = (game.aim_degrees ?? -45) * Math.PI / 180;
    ctx.strokeStyle = colors.ball;
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(ball.x, ball.y);
    ctx.lineTo(ball.x + 70 * fit.scale * Math.cos(angle), ball.y + 70 * fit.scale * Math.sin(angle));
    ctx.stroke();
  }
  ctx.fillStyle = colors.ball;
  ctx.beginPath();
  ctx.arc(ball.x, ball.y, 10 * fit.scale, 0, Math.PI * 2);
  ctx.fill();
  if (game.status === "won" || game.status === "lost") {
    ctx.font = "700 42px -apple-system, BlinkMacSystemFont, sans-serif";
    ctx.textAlign = "center";
    ctx.fillStyle = game.status === "won" ? colors.won : colors.lost;
    ctx.fillText(game.status === "won" ? "¡LLEGASTE!" : "FUERA DEL CAMPO", width / 2, 75);
  }
  ctx.restore();
}
