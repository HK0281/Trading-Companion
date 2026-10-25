import { vi } from "vitest";
import type { Health, LiveProvider, SessionCredentials } from "../src/api";
import { ResearchController } from "../src/controller";
import { credentials, health, mockApi } from "./fixtures";

export const providerHealth: Health = { ...health, provider: "deepseek", data_mode: "tencent",
  development_mode_available: true, available_providers: ["deepseek", "openai"] };

export function providerController() {
  const normal = { ...mockApi(), mode: "normal" as const,
    health: vi.fn(async (): Promise<Health> => ({ ...providerHealth })),
    create: vi.fn(async (provider?: LiveProvider): Promise<SessionCredentials> => ({ ...credentials,
      provider: provider ?? "deepseek", data_mode: "tencent" })),
  };
  const development = { ...mockApi(), mode: "development" as const };
  let next = 0;
  const controller = new ResearchController(normal, () => (++next).toString(16).padStart(32, "0"), development);
  return { normal, development, controller };
}
