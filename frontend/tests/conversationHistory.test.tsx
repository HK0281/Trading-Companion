import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ConnectionError, createApi, type ConversationSummary, type Health, type StoredConversation } from "../src/api";
import App from "../src/App";
import { ResearchController } from "../src/controller";
import { credentials, health, ID, mockApi, reply } from "./fixtures";

const CONVERSATION = "c".repeat(32);
const memoryHealth: Health = { ...health, provider: "deepseek", data_mode: "tencent", memory_enabled: true };
const summary: ConversationSummary = { conversation_id: CONVERSATION, title: "平安银行现在能买吗", provider: "deepseek",
  created_at: "2026-10-30T08:00:00+00:00", updated_at: "2026-10-30T08:05:00+00:00", turns: 2 };
const stored: StoredConversation = { ...summary, turns: [
  { sequence: 1, request_id: "1".repeat(32), asked_at: "2026-10-30T08:00:00+00:00", question: "平安银行现在能买吗", result: reply().result },
  { sequence: 2, request_id: "2".repeat(32), asked_at: "2026-10-30T08:05:00+00:00", question: "中线", result: reply().result }] };
const response = (body: unknown, status = 200) => new Response(body === null ? null : JSON.stringify(body), { status,
  headers: { "Content-Type": "application/json" } });

function memoryController(enabled = true) {
  const api = { ...mockApi(), mode: "normal" as const,
    health: vi.fn(async (): Promise<Health> => ({ ...memoryHealth, memory_enabled: enabled })),
    create: vi.fn(async () => ({ ...credentials, provider: "deepseek" as const, data_mode: "tencent" as const })),
    conversations: vi.fn(async () => enabled ? [{ ...summary }] : null),
    conversation: vi.fn(async () => structuredClone(stored)),
    deleteConversation: vi.fn(async () => {}),
  };
  api.send.mockImplementation(async (_session, id) => ({ ...reply(id), conversation_id: CONVERSATION }));
  let next = 0;
  return { api, controller: new ResearchController(api, () => (++next).toString(16).padStart(32, "0")) };
}

describe("local conversation history", () => {
  it("lists saved conversations only when the service keeps them", async () => {
    const { api, controller } = memoryController();
    await controller.load();
    expect(controller.snapshot().conversations).toEqual([summary]);
    const disabled = memoryController(false);
    await disabled.controller.load();
    expect(disabled.controller.snapshot().conversations).toBeNull();
    expect(api.conversations).toHaveBeenCalledOnce();
  });

  it("opens a saved conversation read-only and continues it in a new session", async () => {
    const { api, controller } = memoryController();
    await controller.load();
    await controller.send("先问一个问题");
    expect(controller.snapshot().conversationId).toBe(CONVERSATION);
    expect(await controller.openConversation(CONVERSATION)).toBe(true);
    expect(api.end).toHaveBeenCalledOnce();
    const opened = controller.snapshot();
    expect(opened.session).toBeNull();
    expect(opened.entries.map((entry) => [entry.question, entry.history])).toEqual([["平安银行现在能买吗", true], ["中线", true]]);
    await controller.send("那现在呢");
    expect(api.create).toHaveBeenLastCalledWith(undefined, CONVERSATION);
    expect(controller.snapshot().entries.map((entry) => entry.question)).toEqual(["平安银行现在能买吗", "中线", "那现在呢"]);
  });

  it("starts unrelated conversations without a conversation id and forgets it on new chat or reset", async () => {
    const { api, controller } = memoryController();
    await controller.load();
    await controller.send("研究平安银行");
    expect(api.create).toHaveBeenLastCalledWith();
    await controller.reset();
    expect(controller.snapshot().conversationId).toBeNull();
    await controller.openConversation(CONVERSATION);
    await controller.end();
    expect(controller.snapshot().conversationId).toBeNull();
    await controller.send("新问题");
    expect(api.create).toHaveBeenLastCalledWith();
  });

  it("deleting the open conversation detaches it", async () => {
    const { api, controller } = memoryController();
    await controller.load();
    await controller.openConversation(CONVERSATION);
    expect(await controller.deleteConversation(CONVERSATION)).toBe(true);
    expect(api.deleteConversation).toHaveBeenCalledWith(CONVERSATION);
    expect(controller.snapshot().conversationId).toBeNull();
    expect(controller.snapshot().entries).toEqual([]);
  });

  it("shows the list, opens an item and confirms deletion in the sidebar", async () => {
    const { api, controller } = memoryController();
    render(<App controller={controller} />);
    const list = await screen.findByRole("region", { name: "历史对话" });
    fireEvent.click(within(list).getByRole("button", { name: /^平安银行现在能买吗/ }));
    await vi.waitFor(() => expect(api.conversation).toHaveBeenCalledWith(CONVERSATION));
    fireEvent.click(within(list).getByRole("button", { name: "删除历史对话：平安银行现在能买吗" }));
    expect(api.deleteConversation).not.toHaveBeenCalled();
    fireEvent.click(within(list).getByRole("button", { name: "确认删除" }));
    await vi.waitFor(() => expect(api.deleteConversation).toHaveBeenCalledWith(CONVERSATION));
    expect(screen.getByText("对话记录保存在本机")).toBeTruthy();
  });
});

describe("conversation history HTTP client", () => {
  it("sends the conversation id only when continuing and checks it is echoed", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response({ ...credentials, conversation_id: CONVERSATION }))
      .mockResolvedValueOnce(response({ ...credentials, conversation_id: null }))
      .mockResolvedValueOnce(response(credentials));
    const api = createApi(fetcher);
    await api.create(undefined, CONVERSATION);
    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({ conversation_id: CONVERSATION });
    await expect(api.create(undefined, CONVERSATION)).rejects.toThrow(ConnectionError);
    await api.create();
    expect(JSON.parse(String(fetcher.mock.calls[2][1]?.body))).toEqual({});
    await expect(api.create(undefined, "not-an-id")).rejects.toThrow(ConnectionError);
  });

  it("validates list and conversation payloads and never uses bearer credentials", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response({ enabled: true, conversations: [summary] }))
      .mockResolvedValueOnce(response({ enabled: false, conversations: [] }))
      .mockResolvedValueOnce(response(stored))
      .mockResolvedValueOnce(response({ ...stored, conversation_id: ID }))
      .mockResolvedValueOnce(response(null, 204));
    const api = createApi(fetcher);
    expect(await api.conversations!()).toEqual([summary]);
    expect(await api.conversations!()).toBeNull();
    expect((await api.conversation!(CONVERSATION)).turns).toHaveLength(2);
    await expect(api.conversation!(CONVERSATION)).rejects.toThrow(ConnectionError);
    await api.deleteConversation!(CONVERSATION);
    expect(fetcher.mock.calls[4]).toEqual([`/v1/conversations/${CONVERSATION}`, expect.objectContaining({ method: "DELETE" })]);
    for (const [, options] of fetcher.mock.calls) expect(options?.headers).toEqual({});
    expect(() => api.conversation!("../x")).toThrow(ConnectionError);
  });

  it("development endpoints never expose history", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const api = createApi(fetcher, "development");
    expect(await api.conversations!()).toBeNull();
    expect(fetcher).not.toHaveBeenCalled();
  });
});
