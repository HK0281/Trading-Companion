import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "../src/App";
import { ConnectionError, createApi, type Health } from "../src/api";
import { ResearchController } from "../src/controller";
import { credentials, health, ID, mockApi, reply } from "./fixtures";

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("startup-pinned source contracts", () => {
  it.each(["synthetic", "tencent", "akshare_eastmoney"] as const)("accepts matching health and credentials for %s", async (data_mode) => {
    const expected = { ...health, provider: "deepseek" as const, data_mode };
    const selected = { ...credentials, provider: "deepseek" as const, data_mode };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(expected)).mockResolvedValueOnce(response(selected, 201));
    const api = createApi(fetcher);
    expect(await api.health()).toEqual(expected);
    expect(await api.create()).toEqual(selected);
    expect(fetcher.mock.calls[1][1]?.body).toBe("{}");
  });
  it.each(["auto", "csv", "eastmoney", { toString: 0 }])("rejects unknown or non-string source %j without coercion", async (data_mode) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ ...health, data_mode }));
    await expect(createApi(fetcher).health()).rejects.toThrow(ConnectionError);
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it("does not put the selected source or a path in per-question HTTP bodies", async () => {
    const selected = { ...credentials, provider: "deepseek" as const, data_mode: "akshare_eastmoney" as const };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(reply(ID)));
    await createApi(fetcher).send(selected, ID, "分析平安银行");
    expect(fetcher.mock.calls[0][1]?.body).toBe(JSON.stringify({ request_id: ID, question: "分析平安银行" }));
    expect(fetcher.mock.calls[0][1]?.body).not.toContain("source");
  });
  it("refuses a source changed between health and session creation", async () => {
    const api = mockApi();
    api.health.mockResolvedValue({ ...health, provider: "deepseek", data_mode: "akshare_eastmoney" });
    api.create.mockResolvedValue({ ...credentials, provider: "deepseek", data_mode: "tencent" });
    const controller = new ResearchController(api);
    await controller.load(); controller.setConsent(true); await controller.start();
    expect(controller.snapshot().session).toBeNull();
    expect(api.send).not.toHaveBeenCalled();
  });
});

async function workspace(data_mode: Health["data_mode"] = "akshare_eastmoney") {
  const api = mockApi();
  api.health.mockResolvedValue({ ...health, provider: "deepseek", data_mode });
  api.create.mockResolvedValue({ ...credentials, provider: "deepseek", data_mode });
  const controller = new ResearchController(api);
  render(<App controller={controller} />);
  await waitFor(() => expect(controller.snapshot().health).not.toBeNull());
  return { api, controller };
}

describe("source-aware workspace", () => {
  it("shows AKShare as the actual source and requires explicit model/data consent", async () => {
    const { api } = await workspace();
    expect(screen.getAllByText("AKShare / 东方财富历史日线").length).toBeGreaterThan(0);
    expect(screen.getByText(/来源由服务启动固定；失败不自动切换/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "研究问题" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("研究问题"), { target: { value: "分析平安银行" } });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    expect(screen.getByText(/股票与日期将用于查询AKShare \/ 东方财富历史日线/)).toBeVisible();
    expect(api.create).not.toHaveBeenCalled(); expect(api.send).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "允许并发送" }));
    await screen.findByText("research_completed"); expect(api.create).toHaveBeenCalledOnce();
    expect(api.send).toHaveBeenCalledOnce();
  });
  it("shows each confirmed research source independently from the server mode", async () => {
    const { api } = await workspace();
    api.send.mockImplementationOnce(async (_session, id) => { const value = reply(id);
      value.result.parameters = { ...value.result.parameters!, source: "akshare_eastmoney" }; return value; });
    fireEvent.change(screen.getByRole("textbox", { name: "研究问题" }), { target: { value: "分析平安银行" } });
    fireEvent.click(screen.getByRole("button", { name: /发送问题/ }));
    fireEvent.click(screen.getByRole("button", { name: "允许并发送" }));
    expect(await screen.findByText("来源：AKShare / 东方财富历史日线")).toBeInTheDocument();
    expect(api.send).toHaveBeenCalledOnce();
  });
  it("keeps Tencent and synthetic labels distinct and offers no in-session source selector", async () => {
    const { api } = await workspace("tencent");
    expect(screen.getAllByText("腾讯历史日线").length).toBeGreaterThan(0);
    expect(screen.getByText(/不能通过对话切换/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /来源/ })).not.toBeInTheDocument();
    expect(screen.queryByText("AKShare / 东方财富历史日线")).not.toBeInTheDocument();
    expect(api.send).not.toHaveBeenCalled();
  });
  it("does not label an online source as synthetic merely because its model is a test script", async () => {
    const api = mockApi(); api.health.mockResolvedValue({ ...health, data_mode: "akshare_eastmoney" });
    render(<App controller={new ResearchController(api)} />);
    await waitFor(() => expect(screen.getAllByText("AKShare / 东方财富历史日线").length).toBeGreaterThan(0));
    expect(screen.getByText(/固定模型脚本 · 查看使用方式/)).toBeInTheDocument();
    expect(screen.queryByText(/人工数据固定在/)).not.toBeInTheDocument();
  });
});
