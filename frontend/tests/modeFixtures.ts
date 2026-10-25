import type { Health } from "../src/api";
import { ResearchController } from "../src/controller";
import { credentials, health, mockApi } from "./fixtures";

export const normalHealth: Health = { ...health, provider: "deepseek", data_mode: "tencent", development_mode_available: true };
export const normalCredentials = { ...credentials, provider: "deepseek" as const, data_mode: "tencent" as const };
export const developmentCredentials = { ...credentials, session_id: "c".repeat(32), access_token: "development-only-capability-123456" };

export function modeController() {
  const normal = { ...mockApi(), mode: "normal" as const }, development = { ...mockApi(), mode: "development" as const };
  normal.health.mockResolvedValue(normalHealth); normal.create.mockResolvedValue(normalCredentials);
  development.health.mockResolvedValue({ ...health, development_mode_available: false });
  development.create.mockResolvedValue(developmentCredentials);
  let next = 0;
  const controller = new ResearchController(normal, () => (++next).toString(16).padStart(32, "0"), development);
  return { normal, development, controller };
}
